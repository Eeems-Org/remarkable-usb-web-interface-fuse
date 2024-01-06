import os
import sys
import fuse

from . import USBWebInterfaceFS


def main():
    server = USBWebInterfaceFS()
    server.parse(values=server, errex=1)
    server.main()


if __name__ == "__main__":
    main()
