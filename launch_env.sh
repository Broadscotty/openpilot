#!/usr/bin/bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export SKIP_FW_QUERY=1
export FINGERPRINT="HOLDEN ASTRA RS-V BK 2017"

if [ -z "$REQUIRED_NEOS_VERSION" ]; then
  export REQUIRED_NEOS_VERSION="19"
fi

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="3"
fi

if [ -z "$PASSIVE" ]; then
  export PASSIVE="1"
fi

export STAGING_ROOT="/data/safe_staging"
