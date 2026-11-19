"""Cooperative cancellation distinct from a processing or file error."""


class OperationCancelled(Exception):
    pass


def check_cancel(event):
    if event is not None and event.is_set():
        raise OperationCancelled('Операция отменена.')
