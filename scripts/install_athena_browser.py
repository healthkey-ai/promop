"""Install and verify the headless browser used by Athena management commands.

The ordinary application environment intentionally omits Playwright and
Chromium. Install ``requirements-athena-scrape.txt`` first, then use this helper
in an operator or CI environment. ``--with-deps`` also installs Linux libraries.
"""
import argparse
import importlib.util
import os
import subprocess
import sys

SMOKE = '''from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content('<title>Athena browser ready</title>')
        assert page.title() == 'Athena browser ready'
    finally:
        browser.close()
print('Athena Chromium launch verified.')
'''


def install(*, with_deps=False):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', '0')
    command = [sys.executable, '-m', 'playwright', 'install', '--only-shell', 'chromium']
    if with_deps:
        command.append('--with-deps')
    subprocess.run(command, check=True)
    # A fresh interpreter checks the same lookup used by management commands.
    # Missing browser binaries or OS libraries must fail the build, not the job.
    subprocess.run([sys.executable, '-c', SMOKE], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-deps', action='store_true', help='Install Linux OS libraries (requires root/sudo)')
    parser.add_argument(
        '--require-package', action='store_true',
        help='Fail when the optional Playwright package is not installed',
    )
    args = parser.parse_args(argv)
    if importlib.util.find_spec('playwright') is None:
        message = 'Playwright is not installed; skipping optional Athena Chromium setup.'
        if args.require_package:
            parser.error(f'{message} Install requirements-athena-scrape.txt first.')
        print(message)
        return
    install(with_deps=args.with_deps)


if __name__ == '__main__':
    main()
