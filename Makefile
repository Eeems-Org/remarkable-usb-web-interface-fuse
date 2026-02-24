.DEFAULT_GOAL := all
VERSION := $(shell grep -m 1 version pyproject.toml | tr -s ' ' | tr -d '"' | tr -d "'" | cut -d' ' -f3)
PACKAGE := $(shell grep -m 1 name pyproject.toml | tr -s ' ' | tr -d '"' | tr -d "'" | cut -d' ' -f3)

OBJ := $(shell find ${PACKAGE} -type f)
OBJ += requirements.txt
OBJ += pyproject.toml
OBJ += README.md

define PLATFORM_SCRIPT
from sysconfig import get_platform
print(get_platform().replace('-', '_'), end="")
endef
export PLATFORM_SCRIPT
PLATFORM := $(shell python -c "$$PLATFORM_SCRIPT")

define ABI_SCRIPT
def main():
    try:
        from wheel.pep425tags import get_abi_tag
        print(get_abi_tag(), end="")
        return
    except ModuleNotFoundError:
        pass

    try:
        from wheel.vendored.packaging import tags
    except ModuleNotFoundError:
        from packaging import tags

    name=tags.interpreter_name()
    version=tags.interpreter_version()
    print(f"{name}{version}", end="")

main()
endef
export ABI_SCRIPT
ABI := $(shell python -c "$$ABI_SCRIPT")

clean:
	if [ -d .venv/mnt ] && mountpoint -q .venv/mnt; then \
		umount -ql .venv/mnt; \
	fi
	git clean --force -dX

build: wheel

release: wheel sdist

install: wheel
	if type pipx > /dev/null; then \
	    pipx install \
	        --force \
	        dist/${PACKAGE}-${VERSION}-${ABI}-${ABI}-${PLATFORM}.whl; \
	else \
	    pip install \
	        --user \
	        --force-reinstall \
	        --no-index \
	        --find-links=dist \
	        ${PACKAGE}; \
	fi

sdist: dist/${PACKAGE}-${VERSION}.tar.gz

wheel: dist/${PACKAGE}-${VERSION}-${ABI}-${ABI}-${PLATFORM}.whl

dist:
	mkdir -p dist

dist/${PACKAGE}-${VERSION}.tar.gz: dist $(OBJ)
	python -m build --sdist

dist/${PACKAGE}-${VERSION}-${ABI}-${ABI}-${PLATFORM}.whl: dist $(OBJ)
	python -m build --wheel


dist/rmuwifuse: dist .venv/bin/activate $(OBJ)
	. .venv/bin/activate; \
	python -m pip install --extra-index-url=https://wheels.eeems.codes/ \
	    wheel \
	    nuitka[onefile]; \
	NUITKA_CACHE_DIR="$(realpath .)/.nuitka" \
	python -m nuitka \
	    --enable-plugin=pylint-warnings \
	    --enable-plugin=upx \
	    --warn-implicit-exceptions \
	    --onefile \
	    --lto=yes \
	    --assume-yes-for-downloads \
	    --python-flag=-m \
	    --remove-output \
	    --output-dir=dist \
	    --output-filename=rmuwifuse \
	    remarkable_usb_web_interface_fuse

.venv/bin/activate: requirements.txt
	@echo "Setting up development virtual env in .venv"
	python -m venv .venv
	. .venv/bin/activate; \
	python -m pip install --extra-index-url=https://wheels.eeems.codes/ -r requirements.txt

dev: .venv/bin/activate $(OBJ)
	if [ -d .venv/mnt ] && mountpoint -q .venv/mnt; then \
		umount -ql .venv/mnt; \
	fi
	mkdir -p .venv/mnt
	. .venv/bin/activate; \
	python -m remarkable_usb_web_interface_fuse \
	    -d \
	    -f \
	    -s \
	    -o auto_unmount \
	    10.11.99.1 \
	    .venv/mnt

test: .venv/bin/activate $(OBJ)
	. .venv/bin/activate; \
	python test.py

executable: .venv/bin/activate dist/rmuwifuse
	dist/rmuwifuse --help

all: release

.PHONY: \
	all \
	build \
	clean \
	dev \
	executable \
	install \
	release \
	sdist \
	wheel \
	test
