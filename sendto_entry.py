"""Windows SendTo entry: accept exactly one input and report misuse visibly."""
import sys

from runtime_streams import initialize_missing_streams
initialize_missing_streams()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    from token_cli import notify_user
    if (len(args) != 2 or args[0] not in ('--auto', '--open', '--auto-item', '--open-item')):
        message = 'Выбери один файл изображения для обработки.'
        print(message, file=sys.stderr)
        notify_user('TokenForge', message, error=True)
        return 2
    from tokenforge import main as application
    return application(args)


if __name__ == '__main__':
    raise SystemExit(main())
