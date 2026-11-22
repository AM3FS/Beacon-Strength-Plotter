"""Store the VKSpotter login in the current user's Windows Credential Manager."""

import ctypes
from ctypes import wintypes
import os

TARGET = 'VK3FS Beacon Strength Plotter/VKSpotter'
GENERIC = 1
PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
BYTE_POINTER = ctypes.POINTER(ctypes.c_ubyte)


class Credential(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', BYTE_POINTER),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]


def read_login():
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('Advapi32', use_last_error=True)
    read = api.CredReadW
    read.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                     ctypes.POINTER(ctypes.POINTER(Credential)))
    read.restype = wintypes.BOOL
    free = api.CredFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = None
    handle = ctypes.POINTER(Credential)()
    if not read(TARGET, GENERIC, 0, ctypes.byref(handle)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        credential = handle.contents
        password = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode('utf-16-le')
        return credential.UserName, password
    finally:
        free(handle)


def save_login(username, password):
    if os.name != 'nt':
        raise OSError('Windows Credential Manager is unavailable.')
    api = ctypes.WinDLL('Advapi32', use_last_error=True)
    write = api.CredWriteW
    write.argtypes = (ctypes.POINTER(Credential), wintypes.DWORD)
    write.restype = wintypes.BOOL
    encoded = password.encode('utf-16-le')
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = Credential(Type=GENERIC, TargetName=TARGET, CredentialBlob=blob,
                            CredentialBlobSize=len(blob), Persist=PERSIST_LOCAL_MACHINE, UserName=username)
    try:
        if not write(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.memset(blob, 0, len(blob))


def forget_login():
    if os.name != 'nt':
        return
    api = ctypes.WinDLL('Advapi32', use_last_error=True)
    delete = api.CredDeleteW
    delete.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    delete.restype = wintypes.BOOL
    if not delete(TARGET, GENERIC, 0):
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise ctypes.WinError(error)
