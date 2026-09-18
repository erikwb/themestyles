"""Filesystem boundaries for untrusted agent output and image decoding."""
import os
import shutil
import tempfile
from pathlib import Path

from files import regular_file
from processes import run

MAX_IMAGE_BYTES = 100 * 1024 * 1024
POLICY = Path(__file__).resolve().with_name("policy.xml")


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


def agent_sandbox(argv, home, workspace, protected, requirements):
    wrapped, environment = sandbox(argv, home, workspace, network=True, readonly=requirements.tool_roots,
                                   overlays=requirements.config_roots, writable=[workspace],
                                   env=requirements.environment)
    # Input mounts must follow the writable workspace mount.
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


def convert_image(source, destination, *, thumbnail=False, log=None, cancel=None):
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
        result = run(argv, env=env, timeout=35, cancel=cancel, log=log, check=False, merge_stderr=False)
        parts = result.stdout.split()
        if result.returncode or len(parts) != 2 or not all(x.isdigit() for x in parts):
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
