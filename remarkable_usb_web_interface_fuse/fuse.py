import os
import errno
import stat
import io

import fuse
import requests

fuse.fuse_python_api = (0, 2)


class FuseArgs(fuse.FuseArgs):
    def __init__(self):
        fuse.FuseArgs.__init__(self)
        self.ipaddress = None

    def __str__(self):
        return (
            "\n".join(
                [
                    f"< {self.ipaddress} on {self.mountpoint}:",
                    f"  {self.modifiers}",
                    "  -o ",
                ]
            )
            + ",\n     ".join(self._str_core())
            + " >"
        )


class FuseOptParse(fuse.FuseOptParse):
    def __init__(self, *args, **kw):
        fuse.FuseOptParse.__init__(self, *args, **kw)

    def parse_args(self, args=None, values=None):
        _opts, _args = fuse.FuseOptParse.parse_args(self, args, values)
        if _args:
            self.fuse_args.ipaddress = _args.pop()
        return _opts, _args


class Stat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0


class FileHandle:
    ipaddress = "10.11.99.1"
    to_upload = {}

    @classmethod
    def post(cls, path, data=None, files=None, timeout=30):
        kwds = {"timeout": timeout}
        if data is not None:
            kwds["data"] = data

        if files is not None:
            kwds["files"] = files

        res = requests.post(f"http://{cls.ipaddress}/{path}", **kwds)
        if "application/json" in res.headers["Content-Type"]:
            try:
                return res.json()

            except requests.exceptions.JSONDecodeError:
                pass

        return res.content

    @classmethod
    def get(cls, path, timeout=30):
        res = requests.get(f"http://{cls.ipaddress}/{path}", timeout=timeout)
        if "application/json" in res.headers["Content-Type"]:
            return res.json()

        return res.content

    def __init__(self, path, flags, *mode):
        self.path = path
        self.flags = flags
        self.mode = mode
        self.stream = None
        self.bytes = None
        self.data = {}
        if path == "/":
            self.data["Type"] = "CollectionType"
            FileHandle._root = self
            return

        for item in self.parent._readdir():
            if item["VissibleName"].strip() == os.path.splitext(self.name)[0]:
                self.data = item

        if self.guid is None:
            self.bytes = io.BytesIO()
            self.to_upload[path] = self
            self.data["Type"] = "DocumentType"

    def __contains__(self, name):
        if not self.is_dir:
            return False

        for _name, _file_type in self.readdir():
            if name == _name:
                return True

        return False

    def __getitem__(self, name):
        if not self.is_dir:
            return None

        for _name, _file_type in self.readdir():
            if name == _name:
                return FileHandle(os.path.join(self.path, name), _file_type)

        return None

    def __len__(self):
        if self.bytes is not None:
            return len(self.bytes.getvalue())

        return int(self.data["sizeInBytes"]) if self.is_file else 0

    @staticmethod
    def root():
        if not hasattr(FileHandle, "_root"):
            FileHandle("/", stat.S_IFDIR | 0o755, 0)

        return FileHandle._root

    @property
    def guid(self):
        return self.data["ID"] if "ID" in self.data else None

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def is_dir(self):
        return self.data["Type"] == "CollectionType"

    @property
    def is_file(self):
        return self.data["Type"] == "DocumentType"

    @property
    def is_open(self):
        return self.stream is not None or self.bytes is not None

    @property
    def parent(self):
        if not hasattr(self, "_parent"):
            self._parent = FileHandle.root().openat(os.path.dirname(self.path))

        return self._parent

    def _readdir(self):
        return self.post(f"documents/{self.guid}", timeout=5)

    def readdir(self):
        if not self.is_dir:
            raise OSError(errno.EBADF, f"{self.path} is not a directory")

        # stat.S_IFDIR if item["Type"] == "CollectionType" else stat.S_IFREG

        yield ".", stat.S_IFDIR
        yield "..", stat.S_IFDIR

        for item in self._readdir():
            file_type = (
                stat.S_IFDIR if item["Type"] == "CollectionType" else stat.S_IFREG
            )
            name = item["VissibleName"]
            if file_type == stat.S_IFREG:
                name += ".pdf"

            yield name, file_type

        for key, item in FileHandle.to_upload.items():
            if os.path.dirname(key) == self.path:
                yield item.name, stat.S_IFREG

    def open(self):
        if not self.is_file:
            raise OSError(errno.EBADF, "Not a file")

        if self.is_open:
            return

        url = f"http://{self.ipaddress}/download/{self.guid}/placeholder"
        res = requests.get(url, timeout=30, stream=True)
        if "content-length" not in res.headers:
            print(res.headers)
            res.close()
            raise OSError(errno.EBADF, "Content-Length missing")

        self.stream = res

    def release(self, _flags):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        self.bytes = None

    def read(self, size, offset):
        assert self.is_open
        assert 0 <= offset < len(self)
        assert size + offset <= len(self)
        if self.bytes is not None:
            if isinstance(self.bytes, io.BytesIO):
                self.bytes.seek(offset)
                return self.bytes.read(size)

            return self.bytes[offset : offset + size]

        raw = self.stream.raw
        if raw.seekable():
            if offset > 0:
                raw.seek(offset)

            return raw.read(size)

        self.bytes = b"".join(list(self.stream.iter_content(1024)))
        self.stream.close()
        self.stream = None
        return self.bytes

    def openat(self, path):
        if not self.is_dir:
            raise OSError(errno.ENOTDIR, f"{self.path} is not a directory")

        path = os.path.normpath(path)
        if path == "/":
            return self.root()

        paths = tuple()
        while True:
            split = os.path.split(path)
            path = split[0]
            if not split[1]:
                break

            paths = (split[1],) + paths

        if not paths:
            raise OSError(errno.ENOTDIR, "Invalid path")

        current = self
        for name in paths:
            if current is None:
                raise OSError(errno.EBADF, "Directory does not exist")

            if not current.is_dir:
                raise OSError(errno.ENOTDIR, f"{current.path} is not a directory")

            current = current[name]

        if current is not None:
            return current

        raise OSError(errno.EBADF, f"{path} does not exist")

    def fgetattr(self):
        _stat = Stat()
        _stat.st_mode = self.flags
        _stat.st_size = len(self)
        return _stat

    def write(self, buf, offset):
        if not isinstance(self.bytes, io.BytesIO):
            raise OSError(errno.EIO, "Not open for writing")

        self.bytes.seek(offset)
        return self.bytes.write(buf)

    def flush(self):
        name = os.path.splitext(self.name)[0]
        if not isinstance(self.bytes, io.BytesIO):
            raise OSError(errno.EEXIST, f"{name}.pdf already exists")

        if f"{name}.pdf" in self.parent:
            raise OSError(errno.EEXIST, f"{name}.pdf already exists")

        self.parent.readdir()
        print(f"Uploading {self.name}")
        res = self.post(
            "upload",
            files={"file": (self.name, self.bytes.getvalue())},
            timeout=120,
        )
        self.bytes.close()
        self.bytes = self.bytes.getvalue()
        if isinstance(res, bytes):
            raise OSError(errno.EIO, f"Invalid JSON response {res}")

        if "error" in res:
            raise OSError(errno.EIO, res["error"])

        if "status" not in res:
            raise OSError(errno.EIO, "Unknown error")

        if res["status"] != "Upload successful":
            raise OSError(errno.EIO, res["status"])


class USBWebInterfaceFS(fuse.Fuse):
    version = "%prog " + fuse.__version__
    fusage = "%prog update_file mountpoint [options]"
    dash_s_do = "setsingle"

    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(
            self,
            *args,
            fuse_args=FuseArgs(),
            parser_class=FuseOptParse,
            **kw,
        )

    @property
    def mountpoint(self):
        return self.fuse_args.mountpoint

    @property
    def ipaddress(self):
        return self.fuse_args.ipaddress

    def fuse_error(self, msg):
        print(msg, file=sys.stderr)
        self.fuse_args.setmod("showhelp")
        fuse.Fuse.main(self, self.args)
        sys.exit(1)

    def main(self, args=None):
        self.args = args
        if self.fuse_args.getmod("showhelp"):
            fuse.Fuse.main(self, args)
            return

        if self.ipaddress is None:
            self.fuse_error("fuse: missing ipaddress parameter")

        FileHandle.ipaddress = self.ipaddress
        self.file_class = FileHandle
        fuse.Fuse.main(self, args)

    def getattr(self, path):
        try:
            return FileHandle.root().openat(path).fgetattr()

        except OSError as err:
            if err.errno == errno.EBADF:
                return -errno.ENOENT

            print(err)
            return -err.errno

    def mknod(self, path, mode, __):
        FileHandle(path, 0, mode)
        return 0

    def opendir(self, path):
        try:
            fh = FileHandle.root().openat(path)
            if not fh.is_dir:
                return -errno.EBADF

            return fh

        except OSError as err:
            print(err)
            return -err.errno

    def releasedir(self, _, fh=None):
        if fh is not None:
            fh.release(0)

        return 0

    def readdir(self, path, _, fh=None):
        # get or create fh
        # if not exists path:
        #    return -errno.ENOENT
        # for name in fh:
        #     yield fuse.Direntry(name)
        try:
            if fh is None:
                fh = FileHandle.root().openat(path)

            if not fh.is_dir:
                return -errno.EBADF

            for name, file_type in fh.readdir():
                yield fuse.Direntry(name, type=file_type)

        except OSError as err:
            print(err)
            return -err.errno
