"""Filesystem boundaries for untrusted agent output and image decoding."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


MAX_IMAGE_BYTES = 100 * 1024 * 1024
POLICY = Path(__file__).resolve().with_name("policy.xml")


@contextmanager
def regular_file(path, *, write=False, limit=None):
    """Check the opened inode before reading or truncating it, never a symlink."""
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags | (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe file: expected a private regular file.")
        if limit is not None and info.st_size > limit:
            raise ValueError("File exceeds the size limit.")
        if write:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "w+b" if write else "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError("The style directory belongs to another user.")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def harden_tree(root):
    """Upgrade old stores without following links or changing outside inodes."""
    private_directory(root)
    for _, _, files, directory_fd in os.fwalk(root, follow_symlinks=False):
        os.fchmod(directory_fd, 0o700)
        for name in files:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid():
                    os.fchmod(fd, info.st_mode & 0o700)
            finally:
                os.close(fd)


def copy_output(source, destination):
    with regular_file(source, limit=MAX_IMAGE_BYTES) as incoming, regular_file(destination, write=True) as outgoing:
        remaining = MAX_IMAGE_BYTES
        while chunk := incoming.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Image exceeds the size limit.")
            outgoing.write(chunk)


def sandbox(argv, home, workspace, *, network=False, readonly=(), overlays=(), writable=(), env=None):
    """Build a private root, PID namespace, home, /tmp and /run; fail closed."""
    binary = shutil.which("bwrap")
    if not binary:
        raise ValueError("Install bubblewrap to generate styles safely (bwrap is missing).")
    command = [binary, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    if network:
        command.append("--share-net")
    for name in ("/usr", "/etc", "/opt", "/bin", "/sbin", "/lib", "/lib64"):
        path = Path(name)
        if path.is_symlink():
            command += ["--symlink", os.readlink(path), name]
        elif path.exists():
            command += ["--ro-bind", name, name]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
                "--dir", str(home), "--dir", "/run/user/" + str(os.getuid())]
    # Some distributions keep the resolver file underneath the hidden /run.
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver.is_file() and not resolver.is_relative_to("/etc"):
        command += ["--ro-bind", str(resolver), str(resolver)]
    for path in dict.fromkeys(map(Path, readonly)):
        if path.exists():
            command += ["--ro-bind", str(path.resolve()), str(path)]
    for path in dict.fromkeys(map(Path, overlays)):
        if path.is_dir():
            command += ["--overlay-src", str(path.resolve()), "--tmp-overlay", str(path)]
        elif path.is_file():
            command += ["--ro-bind", str(path.resolve()), str(path)]
    for path in writable:
        command += ["--bind", str(path), str(path)]
    environment = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
                   "XDG_RUNTIME_DIR": "/run/user/" + str(os.getuid()), "TMPDIR": "/tmp"}
    environment.update(env or {})
    # Environment travels via execve, not bwrap's argv (which is visible in ps).
    return command + ["--chdir", str(workspace), "--", *map(str, argv)], environment


def agent_sandbox(argv, home, workspace, protected, harness):
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    roots = {
        "codex": [Path(os.environ.get("CODEX_HOME", home / ".codex"))],
        "grok": [home / ".grok"],
        "claude": [Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")), home / ".claude.json"],
        "pi": [Path(os.environ.get("PI_CODING_AGENT_DIR", home / ".pi/agent"))],
        "opencode": [config / "opencode", data / "opencode"],
        "muse": [config / "muse"],
        "gemini": [home / ".gemini", config / "gcloud"],
        "copilot": [Path(os.environ.get("COPILOT_HOME", home / ".copilot"))],
        "cursor-agent": [home / ".cursor", config / "cursor"],
        "omp": [Path(os.environ.get("PI_CODING_AGENT_DIR", home / ".omp/agent"))],
        "hermes": [Path(os.environ.get("HERMES_HOME", home / ".hermes"))],
        "openclaw": [home / ".openclaw"],
        "crush": [config / "crush", data / "crush"],
    }[harness]
    # Installed runtimes/tools are readable, but cannot update the host install.
    tools = [home / item for item in (
        ".local/bin", ".local/share/mise/installs", ".local/share/mise/shims", ".local/share/claude",
        ".local/share/cursor-agent", ".bun/bin", ".bun/install/global", ".npm-global", ".opencode/bin")]
    tools.append(Path(argv[0]))
    location_key = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "pi": "PI_CODING_AGENT_DIR",
                    "omp": "PI_CODING_AGENT_DIR", "copilot": "COPILOT_HOME", "hermes": "HERMES_HOME"}.get(harness)
    provider_keys = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
                     "META_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "NOUS_API_KEY")
    selected_keys = {"codex": ("OPENAI_API_KEY",), "grok": ("XAI_API_KEY",),
                     "claude": ("ANTHROPIC_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                     "muse": ("META_API_KEY",), "copilot": ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
                     "cursor-agent": ("CURSOR_API_KEY",)}.get(harness, provider_keys)
    environment = {key: os.environ[key] for key in (
        location_key,
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
        "https_proxy", "http_proxy", "all_proxy", "no_proxy",
        *selected_keys,
    ) if key and key in os.environ}
    environment["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    wrapped, environment = sandbox(argv, home, workspace, network=True, readonly=tools,
                                   overlays=roots, writable=[workspace], env=environment)
    # These bind mounts come after the writable workspace and cannot be replaced.
    index = wrapped.index("--chdir")
    for path in protected:
        wrapped[index:index] = ["--ro-bind", str(path), str(path)]
        index += 3
    return wrapped, environment


def raster_type(path):
    with regular_file(path, limit=MAX_IMAGE_BYTES) as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "WEBP"
    if header.startswith(b"BM"):
        return "BMP"
    raise ValueError("Output is not a supported raster image.")


def convert_image(source, destination, *, thumbnail=False, log=None):
    """Decode only raster coders, without network, home access or host writes."""
    coder = raster_type(source)
    with tempfile.TemporaryDirectory(prefix=".image-", dir=destination.parent) as directory:
        work = Path(directory)
        output = work / ("preview.jpg" if thumbnail else "wallpaper.png")
        args = ["magick", coder + ":" + str(source), "-strip"]
        if thumbnail:
            args += ["-thumbnail", "640x360>"]
        args += ["-format", "%w %h", "-write", str(output), "info:"]
        argv, env = sandbox(args, Path("/image-home"), work, readonly=[source, POLICY], writable=[work],
                            env={"MAGICK_CONFIGURE_PATH": str(POLICY.parent), "MAGICK_TEMPORARY_PATH": str(work)})
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=35, umask=0o077)
        parts = result.stdout.split()
        if result.returncode or len(parts) != 2 or not all(x.isdigit() for x in parts):
            if log is not None:
                with regular_file(log, write=True) as handle:
                    handle.write(result.stderr[:65536].encode())
            raise ValueError("Image decoding failed or exceeded the image security limits.")
        width, height = map(int, parts)
        if not thumbnail and (min(width, height) < 256 or width * height > 64_000_000):
            raise ValueError("Wallpaper must be at least 256 pixels per side and at most 64 megapixels.")
        # A compromised decoder must not be able to return a link to host files.
        # The import staging file is outside its writable mount.
        fd, name = tempfile.mkstemp(prefix=".validated-", dir=destination.parent)
        os.close(fd)
        imported = Path(name)
        try:
            copy_output(output, imported)
            if raster_type(imported) != ("JPEG" if thumbnail else "PNG"):
                raise ValueError("Image conversion returned an unexpected format.")
            imported.replace(destination)
        finally:
            imported.unlink(missing_ok=True)
