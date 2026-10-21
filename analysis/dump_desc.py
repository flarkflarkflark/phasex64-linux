# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
import usb.core, usb.util, libusb_package, usb.backend.libusb1 as lb
be = lb.get_backend(find_library=libusb_package.find_library)
d = usb.core.find(idVendor=0x0a4a, idProduct=0x5b01, backend=be)
print(d)
