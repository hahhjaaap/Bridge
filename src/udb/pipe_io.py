"""Complete writes to unbuffered worker pipes without interleaving frames."""


def write_all(stream, data):
    view = memoryview(data)
    while view:
        count = stream.write(view)
        if not count or count < 0 or count > len(view):
            raise BrokenPipeError("Worker pipe closed during write")
        view = view[count:]
