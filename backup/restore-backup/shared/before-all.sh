#!/bin/bash

echo Hello

tar -xf "/archive/$1" -C /tmp

cp -r /tmp/backup/mc-data/current /backup/mc-data/archived