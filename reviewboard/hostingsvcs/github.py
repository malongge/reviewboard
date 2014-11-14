from __future__ import unicode_literals

import hashlib
import hmac
import json
from collections import defaultdict
from django.conf.urls import patterns, url
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils import six
from django.utils.six.moves.urllib.error import HTTPError, URLError
from django.utils.six.moves.urllib.parse import urljoin
from django.views.decorators.http import require_POST
from reviewboard.admin.server import build_server_url, get_server_url
from reviewboard.hostingsvcs.hook_utils import (close_all_review_requests,
                                                get_git_branch_name,
                                                get_repository_for_hook,
                                                get_review_request_id)
from reviewboard.hostingsvcs.service import (HostingService,
                                             HostingServiceClient)
from reviewboard.scmtools.core import Branch, Commit
from reviewboard.scmtools.errors import FileNotFoundError, SCMError
class GitHubClient(HostingServiceClient):
    def __init__(self, hosting_service):
        super(GitHubClient, self).__init__(hosting_service)
        self.account = hosting_service.account

    #
    # HTTP method overrides
    #

    def http_delete(self, url, *args, **kwargs):
        data, headers = super(GitHubClient, self).http_delete(
            url, *args, **kwargs)
        self._check_rate_limits(headers)
        return data, headers

    def http_get(self, url, *args, **kwargs):
        data, headers = super(GitHubClient, self).http_get(
            url, *args, **kwargs)
        self._check_rate_limits(headers)
        return data, headers

    def http_post(self, url, *args, **kwargs):
        data, headers = super(GitHubClient, self).http_post(
            url, *args, **kwargs)
        self._check_rate_limits(headers)
        return data, headers

    #
    # API wrappers around HTTP/JSON methods
    #

    def api_delete(self, url, *args, **kwargs):
        try:
            data, headers = self.json_delete(url, *args, **kwargs)
            return data
        except (URLError, HTTPError) as e:
            self._check_api_error(e)

    def api_get(self, url, *args, **kwargs):
        try:
            data, headers = self.json_get(url, *args, **kwargs)
            return data
        except (URLError, HTTPError) as e:
            self._check_api_error(e)

    def api_post(self, url, *args, **kwargs):
        try:
            data, headers = self.json_post(url, *args, **kwargs)
            return data
        except (URLError, HTTPError) as e:
            self._check_api_error(e)

    #
    # Internal utilities
    #

    def _check_rate_limits(self, headers):
        rate_limit_remaining = headers.get('X-RateLimit-Remaining', None)

        try:
            if (rate_limit_remaining is not None and
                int(rate_limit_remaining) <= 100):
                logging.warning('GitHub rate limit for %s is down to %s',
                                self.account.username, rate_limit_remaining)
        except ValueError:
            pass

    def _check_api_error(self, e):
        data = e.read()

        try:
            rsp = json.loads(data)
        except:
            rsp = None

        if rsp and 'message' in rsp:
            response_info = e.info()
            x_github_otp = response_info.get('X-GitHub-OTP', '')

            if x_github_otp.startswith('required;'):
                raise TwoFactorAuthCodeRequiredError(
                    _('Enter your two-factor authentication code. '
                      'This code will be sent to you by GitHub.'))

            if e.code == 401:
                raise AuthorizationError(rsp['message'])

            raise HostingServiceError(rsp['message'])
        else:
            raise HostingServiceError(six.text_type(e))


                                 '%(github_public_repo_name)s/'
                                 'issues#issue/%%s',
    supports_post_commit = True
    supports_repositories = True
    has_repository_hook_instructions = True

    client_class = GitHubClient

    repository_url_patterns = patterns(
        '',

        url(r'^hooks/close-submitted/$',
            'reviewboard.hostingsvcs.github.post_receive_hook_close_submitted',
            name='github-hooks-close-submitted')
    )

    REFNAME_PREFIX = 'refs/heads/'
    REFNAME_PREFIX_LEN = len(REFNAME_PREFIX)

        except Exception as e:
            if six.text_type(e) == 'Not Found':
                        _('A repository with this organization or name was '
                          'not found.'))
            rsp, headers = self.client.json_post(
                body=json.dumps(body))
        except (HTTPError, URLError) as e:
                rsp = json.loads(data)
                raise AuthorizationError(six.text_type(e))
                except HostingServiceError as e:
                    if six.text_type(e) != 'Not Found':
            return self.client.http_get(url, headers={
        except (URLError, HTTPError):
            self.client.http_get(url, headers={
        except (URLError, HTTPError):
    def get_branches(self, repository):
        results = []

        url = self._build_api_url(self._get_repo_api_url(repository),
                                  'git/refs/heads')

        try:
            rsp = self.client.api_get(url)
        except Exception as e:
            logging.warning('Failed to fetch commits from %s: %s',
                            url, e)
            return results

        for ref in rsp:
            refname = ref['ref']

            if refname.startswith(self.REFNAME_PREFIX):
                name = refname[self.REFNAME_PREFIX_LEN:]
                results.append(Branch(id=name,
                                      commit=ref['object']['sha'],
                                      default=(name == 'master')))

        return results

    def get_commits(self, repository, branch=None, start=None):
        results = []

        resource = 'commits'
        url = self._build_api_url(self._get_repo_api_url(repository), resource)

        # Note that we don't always use the branch, since the GitHub API
        # doesn't support limiting by branch *and* starting at a SHA. So, the
        # branch argument can be safely ignored if a sha is provided.
        start = start or branch

        if start:
            url += '&sha=%s' % start

        try:
            rsp = self.client.api_get(url)
        except Exception as e:
            logging.warning('Failed to fetch commits from %s: %s',
                            url, e)
            return results

        for item in rsp:
            commit = Commit(
                item['commit']['author']['name'],
                item['sha'],
                item['commit']['committer']['date'],
                item['commit']['message'])
            if item['parents']:
                commit.parent = item['parents'][0]['sha']

            results.append(commit)

        return results

    def get_change(self, repository, revision):
        repo_api_url = self._get_repo_api_url(repository)

        # Step 1: fetch the commit itself that we want to review, to get
        # the parent SHA and the commit message. Hopefully this information
        # is still in cache so we don't have to fetch it again.
        commit = cache.get(repository.get_commit_cache_key(revision))
        if commit:
            author_name = commit.author_name
            date = commit.date
            parent_revision = commit.parent
            message = commit.message
        else:
            url = self._build_api_url(repo_api_url, 'commits')
            url += '&sha=%s' % revision

            try:
                commit = self.client.api_get(url)[0]
            except Exception as e:
                raise SCMError(six.text_type(e))

            author_name = commit['commit']['author']['name']
            date = commit['commit']['committer']['date'],
            parent_revision = commit['parents'][0]['sha']
            message = commit['commit']['message']

        # Step 2: fetch the "compare two commits" API to get the diff if the
        # commit has a parent commit. Otherwise, fetch the commit itself.
        if parent_revision:
            url = self._build_api_url(
                repo_api_url, 'compare/%s...%s' % (parent_revision, revision))
        else:
            url = self._build_api_url(repo_api_url, 'commits/%s' % revision)

        try:
            comparison = self.client.api_get(url)
        except Exception as e:
            raise SCMError(six.text_type(e))

        if parent_revision:
            tree_sha = comparison['base_commit']['commit']['tree']['sha']
        else:
            tree_sha = comparison['commit']['tree']['sha']

        files = comparison['files']

        # Step 3: fetch the tree for the original commit, so that we can get
        # full blob SHAs for each of the files in the diff.
        url = self._build_api_url(repo_api_url, 'git/trees/%s' % tree_sha)
        url += '&recursive=1'
        tree = self.client.api_get(url)

        file_shas = {}
        for file in tree['tree']:
            file_shas[file['path']] = file['sha']

        diff = []

        for file in files:
            filename = file['filename']
            status = file['status']
            try:
                patch = file['patch']
            except KeyError:
                continue

            diff.append('diff --git a/%s b/%s' % (filename, filename))

            if status == 'modified':
                old_sha = file_shas[filename]
                new_sha = file['sha']
                diff.append('index %s..%s 100644' % (old_sha, new_sha))
                diff.append('--- a/%s' % filename)
                diff.append('+++ b/%s' % filename)
            elif status == 'added':
                new_sha = file['sha']

                diff.append('new file mode 100644')
                diff.append('index %s..%s' % ('0' * 40, new_sha))
                diff.append('--- /dev/null')
                diff.append('+++ b/%s' % filename)
            elif status == 'removed':
                old_sha = file_shas[filename]

                diff.append('deleted file mode 100644')
                diff.append('index %s..%s' % (old_sha, '0' * 40))
                diff.append('--- a/%s' % filename)
                diff.append('+++ /dev/null')

            diff.append(patch)

        diff = '\n'.join(diff)

        # Make sure there's a trailing newline
        if not diff.endswith('\n'):
            diff += '\n'

        return Commit(author_name, revision, date, message, parent_revision,
                      diff=diff)

    def get_repository_hook_instructions(self, request, repository):
        """Returns instructions for setting up incoming webhooks."""
        plan = repository.extra_data['repository_plan']
        add_webhook_url = urljoin(
            self.account.hosting_url or 'https://github.com/',
            '%s/%s/settings/hooks/new'
            % (self._get_repository_owner_raw(plan, repository.extra_data),
               self._get_repository_name_raw(plan, repository.extra_data)))

        webhook_endpoint_url = build_server_url(local_site_reverse(
            'github-hooks-close-submitted',
            local_site=repository.local_site,
            kwargs={
                'repository_id': repository.pk,
                'hosting_service_id': repository.hosting_account.service_name,
            }))

        example_id = 123
        example_url = build_server_url(local_site_reverse(
            'review-request-detail',
            local_site=repository.local_site,
            kwargs={
                'review_request_id': example_id,
            }))

        return render_to_string(
            'hostingsvcs/github/repo_hook_instructions.html',
            RequestContext(request, {
                'example_id': example_id,
                'example_url': example_url,
                'repository': repository,
                'server_url': get_server_url(),
                'add_webhook_url': add_webhook_url,
                'webhook_endpoint_url': webhook_endpoint_url,
                'hook_uuid': repository.get_or_create_hooks_uuid(),
            }))

        return self.client.api_post(url=url,
                                    username=client_id,
                                    password=client_secret)
        self.client.api_delete(url=url,
                               headers=headers,
                               username=self.account.username,
                               password=password)
                                  owner, repo_name)
        return self.client.api_get(self._build_api_url(
@require_POST
def post_receive_hook_close_submitted(request, local_site_name=None,
                                      repository_id=None,
                                      hosting_service_id=None):
    """Closes review requests as submitted automatically after a push."""
    hook_event = request.META.get('HTTP_X_GITHUB_EVENT')
    if hook_event == 'ping':
        # GitHub is checking that this hook is valid, so accept the request
        # and return.
        return HttpResponse()
    elif hook_event != 'push':
        return HttpResponseBadRequest(
            'Only "ping" and "push" events are supported.')
    repository = get_repository_for_hook(repository_id, hosting_service_id,
                                         local_site_name)
    # Validate the hook against the stored UUID.
    m = hmac.new(bytes(repository.get_or_create_hooks_uuid()), request.body,
                 hashlib.sha1)
    sig_parts = request.META.get('HTTP_X_HUB_SIGNATURE').split('=')
    if sig_parts[0] != 'sha1' or len(sig_parts) != 2:
        # We don't know what this is.
        return HttpResponseBadRequest('Unsupported HTTP_X_HUB_SIGNATURE')
    if m.hexdigest() != sig_parts[1]:
        return HttpResponseBadRequest('Bad signature.')
    try:
        payload = json.loads(request.body)
    except ValueError as e:
        logging.error('The payload is not in JSON format: %s', e)
        return HttpResponseBadRequest('Invalid payload format')

    server_url = get_server_url(request=request)
    review_request_id_to_commits = \
        _get_review_request_id_to_commits_map(payload, server_url, repository)

    if review_request_id_to_commits:
        close_all_review_requests(review_request_id_to_commits,
                                  local_site_name, repository,
                                  hosting_service_id)

    return HttpResponse()


def _get_review_request_id_to_commits_map(payload, server_url, repository):
    """Returns a dictionary, mapping a review request ID to a list of commits.

    If a commit's commit message does not contain a review request ID, we append
    the commit to the key None.
    """
    review_request_id_to_commits_map = defaultdict(list)

    ref_name = payload.get('ref')
    if not ref_name:
        return None

    branch_name = get_git_branch_name(ref_name)
    if not branch_name:
        return None

    commits = payload.get('commits', [])

    for commit in commits:
        commit_hash = commit.get('id')
        commit_message = commit.get('message')
        review_request_id = get_review_request_id(commit_message, server_url,
                                                  commit_hash, repository)

        review_request_id_to_commits_map[review_request_id].append(
            '%s (%s)' % (branch_name, commit_hash[:7]))

    return review_request_id_to_commits_map