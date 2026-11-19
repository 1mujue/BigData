"""HDFS access for the project’s Compose cluster."""
import getpass
import shlex
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

HDFS_URI = "hdfs://127.0.0.1:8020"


class HdfsStore:
    def __init__(self):
        self.command = ["docker", "compose", "-f", str(Path(__file__).parent / "docker/compose.yaml"), "exec", "-T", "namenode"]

    def dfs(self, *args, input_file=None, decode=True):
        output = subprocess.run([*self.command, "hdfs", "dfs", *args], stdin=input_file,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        return output.decode() if decode else output

    def prepare(self, root):
        self.dfs("-mkdir", "-p", root + "/pdfs", root + "/tables")
        self.dfs("-chown", getpass.getuser(), root + "/tables")

    def put(self, path, destination):
        with Path(path).open("rb") as source:
            self.dfs("-put", "-f", "-", destination, input_file=source)
        if int(self.dfs("-stat", "%b", destination).strip()) != Path(path).stat().st_size:
            raise OSError("HDFS file length differs after upload")

    def put_many(self, paths, directory):
        if not paths:
            return
        with TemporaryDirectory() as temp:
            archive = Path(temp) / "pdfs.tar"
            with tarfile.open(archive, "w") as output:
                for path in paths:
                    output.add(path, arcname=path.name)
            script = ('set -eu; stage=$(mktemp -d /tmp/paper-ingest-XXXXXXXX); '
                      'trap \'rm -rf -- "$stage"\' EXIT; tar -xf - -C "$stage"; '
                      'hdfs dfs -put -f "$stage"/*.pdf ' + shlex.quote(directory))
            with archive.open("rb") as source:
                subprocess.run([*self.command, "sh", "-c", script], stdin=source, capture_output=True, check=True)
        remote = self.dfs("-stat", "%n %b", *(directory + "/" + path.name for path in paths))
        sizes = {name: int(size) for name, size in (line.split() for line in remote.splitlines())}
        if any(sizes.get(path.name) != path.stat().st_size for path in paths):
            raise OSError("HDFS batch length verification failed")

    def sizes(self, directory):
        listing = self.dfs("-ls", directory)
        return {line.split()[-1]: int(line.split()[4]) for line in listing.splitlines() if line.startswith("-")}
