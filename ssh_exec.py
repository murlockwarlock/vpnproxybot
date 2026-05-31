import paramiko
import sys

def execute_ssh_command(host, user, password, command):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=10)
        
        stdin, stdout, stderr = ssh.exec_command(command)
        out = stdout.read().decode()
        err = stderr.read().decode()
        
        ssh.close()
        return out, err
    except Exception as e:
        return "", str(e)

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python3 ssh_exec.py <host> <user> <pass> <command>")
        sys.exit(1)
    
    out, err = execute_ssh_command(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    if out:
        print(out)
    if err:
        print("ERROR:", err, file=sys.stderr)
