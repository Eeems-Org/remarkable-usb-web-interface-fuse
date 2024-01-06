[![remarkable_usb_web_interface_fuse on PyPI](https://img.shields.io/pypi/v/remarkable_usb_web_interface_fuse)](https://pypi.org/project/remarkable_usb_web_interface_fuse)

# reMarkable USB Web Interface FUSE
Userspace filesystem for remarkable usb web interface.

## Usage

```bash
pip install remarkable_usb_web_interface_fuse
rmuwifuse 10.11.99.1 /mnt
```

## Building

```bash
make # Build wheel and sdist packages in dist/
make wheel # Build wheel package in dist/
make sdist # Build sdist package in dist/
make dev # Test mounting 2.15.1.1189 to .venv/mnt
make install # Build wheel and install it with pipx or pip install --user
```
