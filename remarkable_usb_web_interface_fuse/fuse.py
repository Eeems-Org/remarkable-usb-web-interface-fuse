import os
import errno
import stat
import io
import sys

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


class HandleBase:
    ipaddress = "10.11.99.1"
    root = None

    @classmethod
    def init(cls):
        cls.root = DirHandle("/")

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
        self.parent = None
        if not hasattr(self, "data"):
            self.data = {}

        if path != "/":
            self.parent = HandleBase.root.openat(os.path.dirname(path))

    def __len__(self):
        return 0

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def guid(self):
        return self.data["ID"] if "ID" in self.data else None

    @property
    def is_dir(self):
        return False

    @property
    def is_file(self):
        return False

    @property
    def stat(self):
        _stat = Stat()
        _stat.st_mode = self.flags
        _stat.st_size = len(self)
        return _stat

    @property
    def xattrs(self):
        return {}


class DirHandle(HandleBase):
    def __new__(cls, path):
        if path == "/":
            file_handle = super().__new__(cls)
            file_handle.data = {
                "VissibleName": "",
                "ID": "",
                "Type": "CollectionType",
            }
            return file_handle
        name = os.path.splitext(os.path.basename(path))[0]
        for item in HandleBase.root.openat(os.path.dirname(path)).querydir():
            if item["VissibleName"].strip() == name:
                if item["Type"] != "CollectionType":
                    raise OSError(errno.EBADF, f"{path} is not a directory")

                file_handle = super().__new__(cls)
                file_handle.data = item
                return file_handle

        raise OSError(errno.ENOENT, f"{path} does not exist")

    def __init__(self, path):
        super().__init__(path, stat.S_IFDIR | 0o755)

    def __contains__(self, name):
        if not self.is_dir:
            return False

        for _name, _file_type in self.readdir():
            if name == _name:
                return True

        return False

    def __getitem__(self, name):
        path = os.path.join(self.path, name)
        for _name, file_type in self.readdir():
            if name != _name:
                continue

            if file_type == stat.S_IFREG:
                return FileHandle(path, file_type | 0o444, 0)

            return DirHandle(path)

        return None

    @property
    def is_dir(self):
        return True

    def querydir(self):
        return self.post(f"documents/{self.guid}", timeout=5)

    def readdir(self):
        if not self.is_dir:
            raise OSError(errno.EBADF, f"{self.path} is not a directory")

        yield ".", stat.S_IFDIR
        yield "..", stat.S_IFDIR

        for item in self.querydir():
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

    def openat(self, path):
        if not self.is_dir:
            raise OSError(errno.ENOTDIR, f"{self.path} is not a directory")

        path = os.path.normpath(path)
        if path == "/":
            return self.root

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


class FileHandle(HandleBase):
    to_upload = {}

    def __new__(cls, path, flags, *mode):
        if path in cls.to_upload:
            return cls.to_upload[path]

        return super().__new__(cls)

    def __init__(self, path, flags, *mode):
        super().__init__(path, flags, *mode)
        if not hasattr(self, "stream"):
            self.stream = None
            self.bytes = None
            name = os.path.splitext(os.path.basename(path))[0]
            if flags & os.O_CREAT != 0:
                self.data = {"Type": "DocumentType", "VissibleName": name}

        if not self.data:
            for item in self.parent.querydir():
                if item["VissibleName"].strip() != name:
                    continue

                if item["Type"] != "DocumentType":
                    raise OSError(errno.EBADF, f"{path} is not a file")

                self.data = item
                break

            if not self.data:
                raise OSError(errno.ENOENT, f"{path} does not exist")

        if self.guid is None and self.bytes is None:
            self.bytes = io.BytesIO()
            self.to_upload[path] = self
            self.data["Type"] = "DocumentType"

    def __len__(self):
        if not self.is_file:
            return 0

        if "sizeInBytes" in self.data:
            return int(self.data["sizeInBytes"])

        if self.bytes is None:
            return 0

        if isinstance(self.bytes, io.BytesIO):
            return len(self.bytes.getvalue())

        return len(self.bytes)

    @property
    def is_file(self):
        return True

    @property
    def is_open(self):
        return self.stream is not None or self.bytes is not None

    def open(self):
        if not self.is_file:
            raise OSError(errno.EBADF, "Not a file")

    def release(self, _flags=None):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        if isinstance(self.bytes, io.BytesIO):
            self.upload()

        self.bytes = None

    def read(self, size, offset):
        assert 0 <= offset < len(self)
        assert size + offset <= len(self)
        if self.bytes is not None:
            if isinstance(self.bytes, io.BytesIO):
                self.bytes.seek(offset)
                return self.bytes.read(size)

            return self.bytes[offset : offset + size]

        if self.stream is None:
            url = f"http://{self.ipaddress}/download/{self.guid}/placeholder"
            res = requests.get(url, timeout=60, stream=True)
            if "content-length" not in res.headers:
                res.close()
                raise OSError(errno.EBADF, "Content-Length missing")

            self.stream = res

        raw = self.stream.raw
        if raw.seekable():
            if offset > 0:
                raw.seek(offset)

            return raw.read(size)

        self.bytes = b"".join(list(self.stream.iter_content(1024)))
        self.stream.close()
        self.stream = None
        return self.bytes

    def write(self, buf, offset):
        if not isinstance(self.bytes, io.BytesIO):
            raise OSError(errno.EIO, "Not open for writing")

        self.bytes.seek(offset)
        return self.bytes.write(buf)

    def upload(self):
        split = os.path.splitext(self.name)
        name = split[0]
        if not isinstance(self.bytes, io.BytesIO):
            raise OSError(errno.EEXIST, f"{name} bytes are not BytesIO")

        if ".pdf" != split[1] and f"{name}.pdf" in self.parent:
            raise OSError(errno.EEXIST, f"{name}.pdf already exists")

        self.parent.querydir()
        print(f"Uploading {self.name}")
        assert len(self.bytes.getvalue())
        res = self.post(
            "upload",
            files={"file": (self.name, self.bytes.getvalue())},
            timeout=120,
        )
        self.bytes = self.bytes.getvalue()
        if isinstance(res, bytes):
            raise OSError(errno.EIO, f"Invalid JSON response {res}")

        if "error" in res:
            raise OSError(errno.EIO, res["error"])

        if "status" not in res:
            raise OSError(errno.EIO, "Unknown error")

        if res["status"] != "Upload successful":
            raise OSError(errno.EIO, res["status"])

        del self.to_upload[self.path]


class USBWebInterfaceFS(fuse.Fuse):
    version = "%prog " + fuse.__version__
    fusage = "%prog update_file mountpoint [options]"

    def __init__(self, *args, **kw):
        kw["dash_s_do"] = "setsingle"
        fuse.Fuse.__init__(
            self,
            *args,
            fuse_args=FuseArgs(),
            parser_class=FuseOptParse,
            **kw,
        )
        HandleBase.init()

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
        fuse.Fuse.main(self, args)

    def getattr(self, path):
        try:
            return HandleBase.root.openat(path).stat

        except OSError as err:
            if err.errno != errno.EBADF:
                print(err)
                raise

        return -errno.ENOENT

    def create(self, path, flags, mode):
        try:
            HandleBase.root.openat(path)
            return -errno.EEXIST

        except OSError as err:
            if err.errno != errno.EBADF:
                print(err)
                raise

        FileHandle(path, flags, mode)

    def readdir(self, path, _offset):
        for name, file_type in DirHandle(path).readdir():
            yield fuse.Direntry(name, type=file_type)

    def open(self, path, flags):
        try:
            HandleBase.root.openat(path).open()
            return

        except OSError as err:
            if err.errno != errno.EBADF:
                print(err)
                raise

        FileHandle(path, flags).open()

    def release(self, path, flags):
        try:
            HandleBase.root.openat(path).release(flags)
        except OSError as err:
            print(err)
            raise

    def read(self, path, size, offset):
        try:
            return HandleBase.root.openat(path).read(size, offset)
        except OSError as err:
            print(err)
            raise

    def write(self, path, buf, offset):
        try:
            return HandleBase.root.openat(path).write(buf, offset)
        except OSError as err:
            print(err)
            raise

    def setxattr(self, path, name, value, size, flags):
        print(f"setxattr {path} {name} {value} {size} {flags:o}")
        # HandleBase.root.openat(path).xattrs[name] = value
        return -errno.ENOTSUP

    def listxattr(self, path, size):
        print(f"listxattr {path} {size}")
        xattrs = HandleBase.root.openat(path).xattrs.keys()
        if not size:
            return len("".join(xattrs)) + len(xattrs)

        return xattrs

    def getxattr(self, path, name, size):
        print(f"getxattr {path} {name} {size}")
        xattrs = HandleBase.root.openat(path).xattrs
        if name not in xattrs:
            return -errno.ENODATA

        xattr = xattrs[name]
        if not size:
            return len(xattr)

        return xattr

    def unlink(self, path):
        if path not in self.to_upload:
            return -errno.EACCES

        del self.to_upload[path]
