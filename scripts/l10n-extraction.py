#!/bin/env python
"""
L10n extraction maintainance script
===================================

This script is supposed to be running automatically via CircleCI
but can easily be triggered manually too.

This script will do the following:
  - prepare git credentials for pull request push
  - create a new branch (e.g l10n-extract-2017-08-24-0b3bcaf2ca)
  - Update your code
  - Extract new strings and push to the .po files
  - Open a pull request with the new string extractions

If you're running the script manually please make sure to expose
the following variables to the environment:
  - GITHUB_TOKEN (to the github token of addons-robot,
                  talk to tofumatt or cgrebs)
  - TRAVIS_REPO_SLUG="mozilla/addons-server"
  - TRAVIS_BRANCH="master"
"""
import os
import datetime
import subprocess
import glob

import requests

from django.utils.encoding import force_bytes


COMMIT_MESSAGE = 'Extracted l10n messages from {date} at {revision_hash}'
ROBOT_EMAIL = 'addons-dev-automation+github@mozilla.com'
ROBOT_NAME = 'Mozilla Add-ons Robot'
DEBUG_LOCALES = ('dbl', 'dbr')

GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
TRAVIS_REPO_SLUG = os.environ['TRAVIS_REPO_SLUG']
TRAVIS_BRANCH = os.environ['TRAVIS_BRANCH']

GITHUB_HEADERS = {
    'Accept': 'application/vnd.github.v3+json',
    'Authorization': 'token {token}'.format(token=GITHUB_TOKEN)
}

# Make filepaths relative to the locale/ folder
BASE_DIR = os.path.join(
    os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
    'locale')


# Variables we are forwarding to child processes by default.
# We are restricting them for security reasons to not leak any
# keys needlessly.
DEFAULT_ENVIRONMENT_VARIABLES = (
    'NVM_DIR', 'NVM_CD', 'NVM_BIN',
    'NODE_PATH',
    'PATH'
)


class CommandExecutionError(Exception):
    def __init__(self, code, stderr, command):
        lines = (unicode(line) for line in stderr.splitlines())
        message = u'code: {0} stderr: {1}. Command: {2}'.format(
            code, u' '.join(lines), unicode(command))
        super(CommandExecutionError, self).__init__(message)
        self.code = code
        self.stderr = stderr
        self.command = command


def run(command, ignore_out=False, fail_silently=False, **kwargs):
    """Run a command and correctly poll the output and write that to stdout"""
    # Don't automatically merge with os.environ for security reasons.
    # Make this forwarding explicit rather than implicit.
    environ_overrides = kwargs.pop('environ', None)
    shell = kwargs.pop('shell', True)
    process_input = kwargs.pop('process_input', None)

    command = force_bytes(command.format(**kwargs))
    print('running', command)

    environ = {
        key: value for key, value in os.environ.items()
        if key in DEFAULT_ENVIRONMENT_VARIABLES}

    if environ_overrides:
        environ.update(environ_overrides)

    try:
        process = subprocess.Popen(
            command,
            shell=shell,
            universal_newlines=True,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )

        stdout, stderr = process.communicate(input=process_input)
    except OSError as exc:
        raise CommandExecutionError(1, unicode(exc), command)

    if not fail_silently and (stderr and process.returncode != 0):
        raise CommandExecutionError(process.returncode, stderr, command)

    return stdout.strip()


def get_git_revision():
    return run('git rev-parse --short HEAD')


def get_branch_name():
    return 'l10n-extract-{date}-{hash}'.format(
        date=str(datetime.date.today()),
        hash=get_git_revision())


def run_through_nvm(command):
    """
    In case we're using NVM, we need to source it first to make npm/node
    commands work.
    """
    nvm_dir = os.environ.get('NVM_DIR', None)

    if nvm_dir:
        return 'source {nvm_dir}/nvm.sh; {cmd}'.format(
            nvm_dir=nvm_dir, cmd=command)
    return command


def initialize_environment():
    """Cleanup the current git checkout."""
    print('Initializing environment...')
    run('git checkout master')
    run('git checkout -b {branch}', branch=get_branch_name())

    run('make -f Makefile-docker install_python_test_dependencies')
    run('make -f Makefile-docker install_node_js')


def extract_locales():
    print('Extracting locales...')
    print('Extract discovery pane strings...')
    run('python manage.py extract_disco_strings')
    print('Extract Python and template strings...')
    run('python manage.py extract')

    for debug_locale in DEBUG_LOCALES:
        for domain in ('django', 'djangojs'):
            if debug_locale == 'dbr':
                rewrite = 'mirror'
            else:
                rewrite = 'unicode'

            print('Generating debug locale {debug_locale} for {domain} using '
                  '{rewrite}'.format(
                      debug_locale=debug_locale, domain=domain,
                      rewrite=rewrite))

            run(run_through_nvm(
                'npm run potools debug -- --format "{rewrite}" '
                '"{base}/templates/LC_MESSAGES/{domain}.pot" '
                '{base}/{debug_locale}/LC_MESSAGES/{domain}.po'
            ),
                rewrite=rewrite, debug_locale=debug_locale, domain=domain,
                base=BASE_DIR)

    for domain in ('django', 'djangojs'):
        print('Merging new keys for domain {domain}'.format(domain=domain))

        po_files = glob.glob(
            os.path.join(BASE_DIR, '**', '{}.po'.format(domain)))

        for fname in po_files:
            if 'en_US' in fname:
                continue

            print('llllllll', fname)
            run('msguniq --width=200 -o "{fname}" '
                '"{fname}"', fname=fname)
            run('msgmerge --update --width=200 --backup=none '
                '"{fname}" "{base}/templates/LC_MESSAGES/{domain}.pot"',
                domain=domain, fname=fname, base=BASE_DIR)

        created_catalog = run(
            'msgen {base}/templates/LC_MESSAGES/{domain}.pot',
            base=BASE_DIR, domain=domain)

        run('msgmerge --update --width=200 --backup=none '
            '{base}/en_US/LC_MESSAGES/{domain}.po -',
            domain=domain,
            process_input=created_catalog,
            base=BASE_DIR)

        print('Cleaning out obsolete messages for domain {domain}. '
              'See bug 623634 for details.'
              .format(domain=domain))

        for fname in po_files:
            run('msgattrib --no-obsolete --width=200 --no-location '
                '--output-file={fname} {fname}',
                fname=fname)

        print('Convert Seriban text to Latin script')
        run('msgfilter -i {base}/sr/LC_MESSAGES/{domain}.po '
            '-o {base}/sr_Latn/LC_MESSAGES/{domain}.po recode-sr-latin',
            domain=domain,
            base=BASE_DIR)

    print('done.')


def commit_and_push(message):
    run('git commit '
        '-m "{message}" '
        '--author "{robot_name} <{robot_email}>" '
        '--no-gpg-sign '
        '{base}/*/LC_MESSAGES/*.po {base}/templates/',
        message=message,
        robot_name=ROBOT_NAME,
        robot_email=ROBOT_EMAIL,
        base=BASE_DIR)

    run('git push -q '
        '"https://addons-robot:{github_token}@github.com/{repo_slug}/"',
        github_token=GITHUB_TOKEN, repo_slug=TRAVIS_REPO_SLUG)


def create_pull_request(message):
    url = 'https://api.github.com/repos/{repo_slug}/pulls'.format(
        repo_slug=TRAVIS_REPO_SLUG)

    print('Creating the auto merge pull request for {branch}'.format(
        branch=TRAVIS_BRANCH))

    requests.post(url, data={
        'title': message,
        'head': TRAVIS_BRANCH,
        'base': 'master'
    })

    print('Pull request is created...')


def run_extraction():
    commit_message = COMMIT_MESSAGE.format(
        date=str(datetime.date.today()), revision_hash=get_git_revision())

    # initialize_environment()
    # extract_locales()
    commit_and_push(commit_message)
    create_pull_request(commit_message)


if __name__ == '__main__':
    run_extraction()
