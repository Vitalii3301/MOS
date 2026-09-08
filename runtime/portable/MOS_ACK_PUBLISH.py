#!/usr/bin/env python3
import sys

from mos.portable import main


if __name__ == "__main__":
    sys.argv.insert(1, "ack-publish")
    main()
