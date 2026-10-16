#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCBridge CLI 入口 (纯命令行版打包入口, 无 GUI 依赖, 体积小启动快)
功能与 OCBridge.exe 带参数的透传完全一致。
"""
import sys

import bridge

if __name__ == "__main__":
    bridge.main()
