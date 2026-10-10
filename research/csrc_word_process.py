"""Bounded cleanup of a hidden Word instance actually owned by the helper."""
import ctypes
from ctypes import wintypes
from datetime import datetime
def cleanup_owned_word(receipt):
 pid=receipt.get('owned_pid')
 if not pid:return 'no_owned_process'
 if not receipt.get('owned_start_utc'):return 'missing_owned_creation_time_no_termination'
 expected=datetime.fromisoformat(receipt['owned_start_utc'].replace('Z','+00:00')).timestamp()
 k=ctypes.WinDLL('kernel32',use_last_error=True)
 k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
 k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
 k.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4;k.GetProcessTimes.restype=wintypes.BOOL
 k.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)];k.QueryFullProcessImageNameW.restype=wintypes.BOOL
 k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
 k.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];k.TerminateProcess.restype=wintypes.BOOL
 handle=k.OpenProcess(0x1000|0x100000|0x0001,False,int(pid))
 if not handle:return 'already_closed_or_inaccessible_no_termination'
 try:
  creation,exit_time,kernel,user=[wintypes.FILETIME() for _ in range(4)]
  size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
  if not k.GetProcessTimes(handle,ctypes.byref(creation),ctypes.byref(exit_time),ctypes.byref(kernel),ctypes.byref(user)) or not k.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):return 'ownership_query_failed_no_termination'
  timestamp=((creation.dwHighDateTime<<32)|creation.dwLowDateTime)/10000000-11644473600
  if not buffer.value.lower().endswith('\\winword.exe') or abs(timestamp-expected)>=0.001:return 'ownership_mismatch_no_termination'
  if k.WaitForSingleObject(handle,3000)==0:return 'own_instance_quit'
  if k.TerminateProcess(handle,1) and k.WaitForSingleObject(handle,5000)==0:return 'exact_owned_instance_terminated'
  return 'exact_owned_instance_cleanup_failed'
 finally:k.CloseHandle(handle)
