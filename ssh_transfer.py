import paramiko
import sys
import os

def upload_file(host, user, password, local_path, remote_path):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=10)
        
        sftp = ssh.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        
        ssh.close()
        return True, ""
    except Exception as e:
        return False, str(e)

if __name__ == "__main__":
    if len(sys.argv) < 6:
        print("Usage: python3 ssh_transfer.py <host> <user> <pass> <local_path> <remote_path>")
        sys.exit(1)
    
    success, err = upload_file(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    if success:
        print(f"Successfully uploaded {sys.argv[4]} to {sys.argv[5]}")
    else:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
