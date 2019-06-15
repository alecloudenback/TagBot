import os.path
import tarfile
import tempfile

_dir = tempfile.mkdtemp()

if os.path.isfile("resources.tar"):
    with tarfile.TarFile("resources.tar") as tf:
        tf.extractall(_dir)
else:
    print("Resources file was not found")


def resource(path):
    return os.path.join(_dir, path)