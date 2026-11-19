"""A lazy, persistent, killable native-inference process for the desktop GUI.

Only the parent owns export/cache publication and the inference reservation.
The child receives private temporary paths, never user output destinations.
Small pipe messages carry stage/byte progress; pixels travel via files.
"""
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import traceback

from operation_control import OperationCancelled, check_cancel
from local_models import model_candidates, weights_status


def worker_main(connection):
    from runtime_streams import initialize_missing_streams
    initialize_missing_streams()
    session = None
    model = None
    try:
        while True:
            command, args = connection.recv()
            name = args[0]

            def emit(stage, count=0, total=0, speed=0.0):
                connection.send(('stage', (name, stage, count, total, speed)))

            try:
                if command == 'download':
                    from model_download import download_model
                    download_model(name, args[1], emit)
                elif command == 'initialize':
                    emit('initialize')
                    from local_models import new_local_session
                    if weights_status(name) != 'local':
                        raise OSError('Файл модели недоступен. Повтори загрузку.')
                    session = new_local_session(name)
                    model = name
                    emit('ready')
                elif command == 'extract':
                    if session is None or model != name:
                        raise RuntimeError('Нейросеть не инициализирована.')
                    emit('extract')
                    from PIL import Image
                    from token_engine import remove_background
                    with Image.open(args[1]) as input_image:
                        result = remove_background(input_image, session, args[3])
                    result.save(args[2], format='PNG', compress_level=0)
                else:
                    raise ValueError('Unknown inference command')
                connection.send(('ok', None))
            except (Exception, SystemExit) as exc:
                connection.send(('error', (str(exc), traceback.format_exc())))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class InferenceClient:
    """Used by one serialized GUI worker; idle shutdown runs on Tk's thread."""
    def __init__(self, cancel_event, emit, *, context=None, target=worker_main, log=None):
        self.cancel_event = cancel_event
        self.emit = emit
        self.context = context or mp.get_context('spawn')
        self.target = target
        self.log = log or (lambda message: None)
        self.process = None
        self.connection = None
        self.loaded = None

    def _start(self):
        check_cancel(self.cancel_event)
        if self.process is not None and self.process.is_alive():
            return
        self.close()
        parent, child = self.context.Pipe()
        process = self.context.Process(target=self.target, args=(child,), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            process.close()
            raise
        child.close()
        self.process, self.connection = process, parent

    def close(self):
        # Terminate native code only, not the thread that owns publication.
        # Never release the reservation before the child is confirmed dead.
        if self.process is not None:
            process = self.process
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                if process.is_alive():
                    raise RuntimeError('Не удалось остановить процесс нейросети.')
            process.join()
            process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.loaded = None

    def _call(self, command, *args):
        try:
            self._start()
            self.connection.send((command, args))
            while True:
                check_cancel(self.cancel_event)
                if self.connection.poll(.1):
                    kind, value = self.connection.recv()
                    if kind == 'stage':
                        self.emit(*value)
                    elif kind == 'ok':
                        check_cancel(self.cancel_event)
                        return
                    elif kind == 'error':
                        message, detail = value
                        self.log(detail)
                        raise RuntimeError(message or 'Ошибка нейросети.')
                    else:
                        raise RuntimeError('Некорректный ответ процесса нейросети.')
                elif not self.process.is_alive():
                    raise RuntimeError(f'Процесс нейросети завершился (код {self.process.exitcode}).')
        except (EOFError, BrokenPipeError) as exc:
            self.close()
            raise RuntimeError('Процесс нейросети неожиданно завершился.') from exc
        except BaseException:
            self.close()
            raise

    def prepare(self, name):
        check_cancel(self.cancel_event)
        status = weights_status(name)
        if status == 'local':
            return
        if status == 'invalid':
            raise OSError('Файл модели повреждён или пуст. Проверь папку моделей.')
        self.close()
        destination = model_candidates(name)[0]
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Only this instance knows the random path. Cleanup after the child
        # exits on cancellation, including abrupt native failures.
        fd, staging = tempfile.mkstemp(prefix='.tokenforge-', suffix='.part', dir=destination.parent)
        os.close(fd)
        try:
            self._call('download', name, staging)
        finally:
            Path(staging).unlink(missing_ok=True)
        self.emit(name, 'local', 0, 0, 0.0)

    def session(self, name):
        # A request to initialize means the revision changed or the parent
        # discarded the old session. Start fresh to release native memory.
        self.close()
        self.prepare(name)
        self._call('initialize', name)
        self.loaded = name
        return name

    def extract(self, image, name, edge_mode):
        check_cancel(self.cancel_event)
        if self.loaded != name or self.process is None or not self.process.is_alive():
            self.session(name)
        from PIL import Image
        self.emit(name, 'transfer', 0, 0, 0.0)
        with tempfile.TemporaryDirectory(prefix='TokenForge inference ') as directory:
            source = Path(directory) / 'input.png'
            output = Path(directory) / 'output.png'
            image.save(source, format='PNG', compress_level=0)
            check_cancel(self.cancel_event)
            self._call('extract', name, str(source), str(output), edge_mode)
            with Image.open(output) as result:
                if result.size != image.size or result.mode != 'RGBA':
                    raise RuntimeError('Некорректный результат нейросети.')
                return result.copy()
