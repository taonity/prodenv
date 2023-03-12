#!/bin/bash

echo Hello

tar -xf /archive/backup-2023-03-12T17-52-00.tar.gz -C /tmp

cp -r /tmp/backup/prodenv-mc-data/current /backup/prodenv-mc-data/archived