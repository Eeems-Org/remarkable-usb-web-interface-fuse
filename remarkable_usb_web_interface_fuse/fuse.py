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

    @classmethod
    def getItems(cls, parent=""):
        for item in cls.post(f"documents/{parent}", timeout=5):
            yield FileHandle(item)

    def __init__(self, data):
        self.data = data
        self.stream = None
        self.bytes = None

    def __contains__(self, name):
        if not self.is_dir:
            return False

        for item in self.readdir():
            if item.name == name:
                return True

        return False

    def __getitem__(self, name):
        if not self.is_dir:
            return None

        for item in self.readdir():
            if item.name == name:
                return item

        return None

    def __len__(self):
        return int(self.data["sizeInBytes"]) if self.is_file else 0

    @staticmethod
    def root():
        return FileHandle({"VissibleName": "", "Type": "CollectionType", "ID": ""})

    @property
    def guid(self):
        return self.data["ID"]

    @property
    def name(self):
        name = self.data["VissibleName"].strip()
        if self.is_dir:
            return name

        # TODO - maybe expose filetype?
        #        pdf, epub, notebook
        return f"{name}.pdf"

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
    def mode(self):
        return stat.S_IFDIR | 0o755 if self.is_dir else stat.S_IFREG | 0o666

    def readdir(self):
        if not self.is_dir:
            raise OSError(errno.EBADF, "Not a directory")

        for item in self.getItems(self.guid):
            yield item

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

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        self.bytes = None

    def read(self, size=0, offset=0):
        assert self.is_open
        assert 0 <= offset < len(self)
        if self.bytes is not None:
            return self.bytes[offset : offset + size]

        if size <= 0:
            size = len(self)

        assert size + offset <= len(self)

        raw = self.stream.raw
        if raw.seekable():
            if offset > 0:
                raw.seek(offset)

            return raw.read(size)

        self.bytes = b"".join([c for c in self.stream.iter_content(1024)])
        self.stream.close()
        self.stream = None
        return self.bytes

    def openat(self, path):
        if not self.is_dir:
            raise OSError(errno.ENOTDIR, "Not a directory")

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
            if not current.is_dir:
                raise OSError(errno.ENOTDIR, "Not a directory")

            current = current[name]
            if current is None:
                raise OSError(errno.EBADF, "File does not exist")

        return current

    def stat(self):
        _stat = Stat()
        _stat.st_mode = self.mode
        _stat.st_size = len(self)
        return _stat


class UploadFileHandle:
    def __init__(self, path, mode):
        self.mode = mode
        self.path = path
        self.parent = FileHandle.root().openat(os.path.dirname(path))
        name = os.path.splitext(self.name)[0]
        if f"{name}.pdf" in self.parent:
            raise OSError(errno.EEXIST, f"{name}.pdf already exists")

        self.data = io.BytesIO()
        self.has_written = False
        FileHandle.to_upload[path] = self

    @property
    def name(self):
        return os.path.basename(self.path)

    def seek(self, offset):
        self.data.seek(offset)

    def write(self, data):
        self.data.write(data)
        self.has_written = True

    def close(self):
        if not self.has_written:
            return

        del FileHandle.to_upload[self.path]
        name = os.path.splitext(self.name)[0]
        if f"{name}.pdf" in self.parent:
            raise OSError(errno.EEXIST, f"{name}.pdf already exists")

        self.parent.readdir()
        res = self.parent.post(
            "upload",
            files={"file": (self.name, self.data.getvalue())},
        )
        self.data.close()
        if isinstance(res, bytes):
            raise OSError(errno.EIO, f"Invalid JSON response {res}")

        if "error" in res:
            raise OSError(errno.EIO, res["error"])

        if "status" not in res:
            raise OSError(errno.EIO, "Unknown error")

        if res["status"] != "Upload successful":
            raise OSError(errno.EIO, res["status"])

    def stat(self):
        _stat = Stat()
        _stat.st_mode = self.mode
        _stat.st_size = len(self.data.getvalue())
        return _stat


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
        fuse.Fuse.main(self, args)

    # def statfs(self):
    #     struct = fuse.StatVfs()
    #     struct.f_bsize = 0
    #     struct.f_frsize = 0
    #     struct.f_blocks = 0
    #     struct.f_bfree = 0
    #     struct.f_bavail = 0
    #     struct.f_files = 0
    #     struct.f_ffree = 0
    #     struct.f_favail = 0
    #     struct.f_flag = 0
    #     struct.f_namemax = 0
    #     return struct

    def getattr(self, path, fh=None):
        if path in FileHandle.to_upload:
            return FileHandle.to_upload[path].stat()

        try:
            if fh is None:
                fh = FileHandle.root().openat(path)

            return fh.stat()

        except OSError as err:
            if err.errno == errno.EBADF:
                return -errno.ENOENT

            print(err)
            return -err.errno

    def mknod(self, path, mode, __):
        UploadFileHandle(path, mode)
        return 0

    def open(self, path, flags):
        if path in FileHandle.to_upload:
            mode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
            if (flags & mode) != os.O_WRONLY:
                return -errno.EACCES

            return FileHandle.to_upload[path]

        try:
            fh = FileHandle.root().openat(path)
            if not fh.is_file:
                return -errno.EACCES

            mode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
            if (flags & mode) != os.O_RDONLY:
                return -errno.EACCES

            return fh

        except OSError as err:
            print(err)
            return -err.errno

    def release(self, path, __, fh=None):
        if fh is not None:
            fh.close()

        if path in FileHandle.to_upload:
            FileHandle.to_upload[path].close()

        return 0

    def read(self, path, size, offset, fh=None):
        try:
            if fh is None:
                fh = FileHandle.root().openat(path)

            if not fh.is_open:
                fh.open()

            return fh.read(size, offset)

        except OSError as err:
            print(err)
            return -err.errno

    def write(self, path, buf, offset, fh=None):
        if fh is None and fh in FileHandle.to_upload:
            fh = FileHandle.to_upload[path]

        if not isinstance(fh, UploadFileHandle):
            try:
                if fh is None:
                    fh = FileHandle.root().openat(path)

                if fh is not None:
                    errno.EEXIST

            except OSError as err:
                if err.errno != errno.EBADF:
                    print(err)
                    return -err.errno

        fh = UploadFileHandle(path, 0o666)
        fh.seek(offset)
        fh.write(buf)

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
            fh.close()

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

            yield fuse.Direntry(".")
            yield fuse.Direntry("..")
            for item in fh.readdir():
                yield fuse.Direntry(item.name)

            for key in FileHandle.to_upload:
                if os.path.dirname(key) == path:
                    yield fuse.Direntry(key)

        except OSError as err:
            print(err)
            return -err.errno
