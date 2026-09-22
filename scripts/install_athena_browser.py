"""Install and verify the headless browser used by Athena management commands.

Render installs into the Python package (PLAYWRIGHT_BROWSERS_PATH=0) so its browser
survives the build-to-runtime handoff. Docker/CI use --with-deps to also install
Linux libraries; native Render builds use the platform's installed libraries.
"""
import argparse
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-deps', action='store_true', help='Install Linux OS libraries (requires root/sudo)')
    install(with_deps=parser.parse_args().with_deps)
