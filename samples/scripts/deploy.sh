#!/usr/bin/env bash
set -e

build() {
  echo "building"
  compile_assets
}

compile_assets() {
  echo "assets" # $# not comment
}

deploy() {
  build
  upload "$1"
}

upload() { echo "upload $1"; }

deploy prod
