"""The plugin's contract with the bundled bitbang binary.

The Go proxy is the listener -- it owns the data channel, the signaling,
and the PIN; the Python adapter only feeds it video. So the plugin's
real dependency is an argv built in _plugin.py against a CLI that lives
in another repository, on its own release cycle. Nothing else notices
when a flag there is renamed, and the failure lands on a Pi at startup.

Skipped unless BITBANG_BIN points at a binary, so a plain `pytest` still
works without a Go toolchain. CI builds bitbang-cli main and sets it.
"""

import os
import re
import subprocess
import tempfile

import pytest

BIN = os.environ.get('BITBANG_BIN')
pytestmark = pytest.mark.skipif(not BIN, reason='BITBANG_BIN not set')

# Every flag _supervise_go passes that is documented in --help. Keep in step
# with the argv there. -video-fd is deliberately absent: it is internal
# plumbing, hidden from help, and covered by the full-argv parse test below --
# which is the stronger check anyway.
PROXY_FLAGS = [
    '-program',
    '-v',
    '-proxy-client-ip',
    '-pin',
]


def _help(*args):
    r = subprocess.run([BIN, *args, '--help'], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


def test_the_binary_runs():
    r = subprocess.run([BIN, 'version'], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f'`bitbang version` failed: {r.stderr}'
    assert re.search(r'\d+\.\d+', r.stdout + r.stderr), 'no version in the output'


@pytest.mark.parametrize('flag', PROXY_FLAGS)
def test_serve_proxy_still_accepts(flag):
    """A renamed flag is a startup crash on every Pi, and the only place
    the two repos are joined is this argv."""
    text = _help('serve', 'proxy')
    assert flag in text, f'`serve proxy` no longer documents {flag}:\n{text}'


def test_the_full_argv_is_accepted():
    """Flags present in help is not the same as the combination parsing.

    And --help is not enough on its own: it exits inside flag parsing,
    before the positional grammar is read at all, so `serve proxy
    <target>` -- where the target now lives -- goes unchecked. `serve
    proxy frobnicate --help` exits 0.

    So run the real argv instead. The CLI exits 2 when it rejects an
    argument, flag or positional, and something other than 2 once it is
    past parsing and into starting up. -video-fd 3 pointed at /dev/null
    is what makes that quick: the fd is not a socket, so it gets far
    enough to prove the argv parsed and then stops on its own.
    """
    rc, out = _run_argv('localhost:5000')
    assert rc != 2, f'the CLI rejected the argv the plugin builds:\n{out}'
    assert 'is not something to serve' not in out, out


def test_a_bogus_positional_is_still_caught():
    """The guard on the guard. If the check above ever stops running the
    grammar -- back to --help, say -- this is what notices, because a
    word the CLI cannot possibly accept has to come back as a rejection.
    """
    rc, out = _run_argv('localhost:5000', 'frobnicate')
    assert rc == 2, f'a bogus positional was accepted (rc={rc}):\n{out}'


def _run_argv(*positionals):
    """Run the plugin's argv with extra positionals, returning (rc, output).

    A None rc means it was still running at the timeout, which counts as
    accepted -- the CLI rejects argv long before it gets that far.
    """
    p = subprocess.Popen(
        [BIN, 'serve', 'proxy', *positionals,
         '-program', 'octoprint',
         '-v', '-video-fd', '3', '-proxy-client-ip',
         '-pin', '1234', '-ephemeral'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        pass_fds=(), env=dict(os.environ, HOME=tempfile.mkdtemp()))
    try:
        out = p.communicate(timeout=20)[0]
        return p.returncode, out
    except subprocess.TimeoutExpired:
        p.kill()
        return None, p.communicate()[0]
