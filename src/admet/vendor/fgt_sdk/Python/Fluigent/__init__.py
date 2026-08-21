import sys

if sys.version_info.major == 3 and sys.version_info.minor < 9:
    __import__('pkg_resources').declare_namespace(__name__)
else:
    __path__ = __import__('pkgutil').extend_path(__path__, __name__)
