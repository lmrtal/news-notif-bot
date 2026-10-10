"""命令行入口。计划任务调用的就是这个文件，实现在 notif 包里。

  python main.py [--scope all|bili|news]
  python main.py --force
  python main.py --test-push
  python main.py --reset
"""
from notif.cli import main

if __name__ == "__main__":
    main()
