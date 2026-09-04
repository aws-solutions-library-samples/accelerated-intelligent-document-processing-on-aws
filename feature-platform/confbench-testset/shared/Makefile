# SAM `BuildMethod: makefile` target for the shared layer.
#
# Why a makefile instead of `BuildMethod: python3.12`: that build method runs
# PythonPipBuilder, which installs a requirements.txt and does NOT copy
# hand-authored source — variants.py would be dropped from the layer. And with
# no BuildMethod at all, SAM leaves ContentUri as a relative path pointing
# outside .aws-sam/build, which breaks `sam package`.
#
# The layer also carries pyarrow, which the PythonPipBuilder CANNOT install for
# this feature's arm64 functions. That builder asks pip for exactly one platform
# tag — `--platform manylinux2014_aarch64` — and pyarrow stopped publishing
# aarch64 wheels under that tag at 21.0.0 (20.0.0 is the last one); from 21.0.0
# on it ships `manylinux_2_28_aarch64` only. So a cross-arch build on an x86_64
# host fails with `PythonPipBuilder:ResolveDependencies - {pyarrow==...(wheel)}`.
# Downgrading is not an option: pyarrow <= 23.0.0 is affected by CVE-2026-25087
# (use-after-free, HIGH), fixed in the 23.0.1 pinned repo-wide. Installing it
# here lets us name the modern tag explicitly, which is what the python3.12
# runtime actually wants — Lambda runs it on AL2023 (glibc 2.34), well past the
# 2.28 floor. Keeping it in the layer rather than in each function's
# requirements.txt also means one copy for the four functions sharing ingest/.
#
# The pip install is followed by prune_layer.py, which strips the ~35 MB of
# pyarrow that `pyarrow.parquet` cannot reach at runtime and then FAILS THE BUILD
# if the layer is still too large to combine with the pre-2026-08-13 ingest code
# during an in-place stack update. That transition, not the steady state, is the
# binding size constraint — see prune_layer.py's module docstring.
#
# ARTIFACTS_DIR is supplied by SAM. Lambda puts a layer's `python/` directory on
# sys.path, so the module must land at $(ARTIFACTS_DIR)/python/variants.py.
build-SharedLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	cp python/variants.py "$(ARTIFACTS_DIR)/python/variants.py"
	python3 -m pip install \
		--requirement requirements.txt \
		--target "$(ARTIFACTS_DIR)/python" \
		--platform manylinux_2_28_aarch64 \
		--python-version 3.12 \
		--implementation cp \
		--only-binary=:all: \
		--no-compile \
		--upgrade
	python3 prune_layer.py "$(ARTIFACTS_DIR)"
