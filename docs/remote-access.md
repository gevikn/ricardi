# Reaching the box

A machine at home, behind NAT, that has to come back on its own after a power
cut and be reachable from somewhere else entirely. [`running.md`](running.md)
covers keeping Ricardi alive once the box is up. This covers the box.

Three links have to hold, and each one has to hold with nobody logged in:

    host boots  ->  hypervisor starts the VM  ->  the VM dials out

The last one is the trick. Nothing here opens a port, forwards anything, or
needs a public address — the VM makes an outbound connection and traffic comes
back down it. That is the same shape Ricardi already uses for Slack, which is
why neither needs a hostname pointed at your house.

This was written on Windows 11 Pro + VMware Workstation 26 + Ubuntu, with
Cloudflare Tunnel. The reasoning transfers; the menus will not.

## Fill these in first

| | Yours |
|---|---|
| VM static address | `192.168.___.10` |
| NAT gateway | `192.168.___.2` |
| Hostname | `___.example.com` |
| Guest user | `___` |
| Tunnel UUID | from step 4.2 |

The third octet comes from the host: `ipconfig` and read the *VMware Network
Adapter VMnet8* address, which is `192.168.X.1`. The gateway is `.2` and
VMware's DHCP pool starts at `.128`, so anything from `.3` to `.127` is yours.

---

## 1. The host starts the VM with nobody logged in

The old way to do this was `AutoAdminLogon` with `DefaultPassword` in the
registry, which stores an account password in plaintext where anything on the
box can read it. Don't. Workstation 17+ ships an autostart service that runs
VMs from a service context instead, which is the thing autologon was being
abused to fake.

**Two conditions kill this before you start.** An encrypted VM cannot
autostart — there is a passphrase and nobody to type it:

```powershell
Select-String -Path "<path>\*.vmx" -Pattern "encryption"
```

And a BitLocker volume that wants a PIN at boot means the host never comes back
unattended at all, hypervisor be damned:

```powershell
manage-bde -status
```

Check both first. They invalidate everything below.

**Run Workstation elevated.** VM → Power Actions → tick Auto Start. The config
lives in a machine-level file:

```
C:\ProgramData\VMware\VMware Workstation\vmautostart.xml
```

whose ACL is `Everyone:(R)` and Full only for `Administrators` and `SYSTEM`.
UAC strips the Administrators SID from an unelevated token, so a normal
Workstation window matches only `Everyone`, happily ticks the box, and fails at
write time with *"Failed to update AutoStart configuration."* Elevation is the
fix. **Do not widen the ACL** — anything that can write that file can register
a VM to launch at boot as you.

Confirm it took, and keep the before/after:

```powershell
Get-Content "C:\ProgramData\VMware\VMware Workstation\vmautostart.xml"
```

You want a real `<e id="0">` block with your `.vmx` path, *outside* the comment
the sample ships wrapped in.

**Then the service**, which is the half the dialog nags about and the half
people skip:

```powershell
Get-Service *autostart* | Format-List Name, DisplayName, Status, StartType
```

`services.msc` → **Startup type: Automatic**, and **Log On** → *This account* →
the user that owns the VMs. Not `LocalSystem`: VM registration lives in that
user's Workstation profile. The password is kept as an LSA secret rather than
plaintext in the registry, which is the whole improvement over autologon.

**Test it in ten seconds instead of three minutes.** VMs start when the host
boots *or whenever the service starts*:

```powershell
Restart-Service "<service name>"
```

If the VM powers on, the config and the logon account are both right.

Expect the console in your interactive Workstation window to be blank or
missing afterwards. The VM is running in another context; it is not dead. Stop
diagnosing from that window — everything below is how you get a real way in.

## 2. An address that does not move

A DHCP lease that moves breaks every SSH target you write down. Look at what
netplan already has, because a desktop image and a server image disagree:

```bash
ls -la /etc/netplan/
sudo cat /etc/netplan/*.yaml
```

Typically there are two files that merge: the installer's (your interface,
DHCP on, pinned to a MAC) and one that sets `renderer: NetworkManager`
globally. Netplan merges them per key in filename order, so add a file that
sorts last and overrides only what you need:

```bash
sudo tee /etc/netplan/99-static.yaml > /dev/null <<'YAML'
network:
  version: 2
  ethernets:
    ens33:
      dhcp4: no
      dhcp6: no
      addresses: [192.168.X.10/24]
      routes:
        - to: default
          via: 192.168.X.2
      nameservers:
        addresses: [192.168.X.2, 1.1.1.1]
YAML
sudo chmod 600 /etc/netplan/99-static.yaml
```

Substitute your real octet — a literal `X` fails validation, which is at least
a loud failure. `match` and `set-name` from the installer file still apply;
you are only overriding the DHCP keys and adding an address. Turn `dhcp6` off
too or you keep a second address that confuses you later about which is real.

```bash
sudo netplan get      # the merged, effective config
sudo netplan apply
ip -br addr show ens33
```

**With the NetworkManager renderer, `netplan apply` often leaves the interface
up with no address**, and `netplan try` prints a complaint about
`systemd-networkd` not running. Both have the same cause: netplan's rollback
and reload paths are talking to a backend that isn't driving your interface.

```bash
sudo systemctl restart NetworkManager
ip -br addr show ens33
```

Two consequences. First, **do not trust `netplan try`'s 120-second rollback on
this box** — verify with `ip -br addr` after every change instead. Second, if
this keeps fighting you, switch the renderer to `networkd`, which is the right
backend for a static-IP headless VM anyway:

```bash
sudo sed -i 's/^  version: 2/  version: 2\n  renderer: networkd/' /etc/netplan/99-static.yaml
sudo systemctl enable --now systemd-networkd
sudo systemctl disable --now NetworkManager
sudo netplan apply
```

Verify end to end before moving on:

```bash
ping -c2 192.168.X.2          # the NAT gateway
ping -c2 1.1.1.1              # out
getent hosts archive.ubuntu.com
```

Then **reboot and check the address came back without you restarting
anything** — that is the property you actually need, and a manual
`systemctl restart` proves nothing about boot.

```bash
sudo systemctl reboot -i
```

The `-i` ignores inhibitors. A running desktop session holds one, so a plain
`reboot` refuses with *"Operation inhibited by … user session inhibited"*.

## 3. SSH on the LAN first

Prove the easy hop before adding a tunnel to debug at the same time.

```bash
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
systemctl is-active ssh
```

From the host:

```powershell
ssh <user>@192.168.X.10
```

Until this works, nothing in step 4 can.

## 4. The tunnel

### 4.1 Authenticate

```bash
cloudflared tunnel login
```

Prints a URL and waits. Open it anywhere — another machine is fine. Writes
`~/.cloudflared/cert.pem`.

### 4.2 Create the tunnel, then route it

```bash
cloudflared tunnel create ricardi
cloudflared tunnel list
```

Keep the UUID. Then:

```bash
cloudflared tunnel route dns ricardi ricardi.example.com
```

**This is where the DNS record comes from. You do not create an A record.**
An A record points a name at an IP, and there is no IP — the VM is on private
NAT and nothing listens. What this writes is:

```
ricardi.example.com   CNAME   <UUID>.cfargotunnel.com   (Proxied)
```

which Cloudflare resolves internally to whichever edge node currently holds
your tunnel. If you make it by hand instead, the proxy **must** be on; grey-
clouded, `cfargotunnel.com` means nothing outside Cloudflare's network.

Run `route dns` before `tunnel create` and you get *"ricardi is neither the ID
nor the name of any of your tunnels"* — the ordering is not cosmetic.

### 4.3 Configure and install

```bash
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml > /dev/null <<'YAML'
tunnel: PASTE-UUID
credentials-file: /etc/cloudflared/PASTE-UUID.json
ingress:
  - hostname: ricardi.example.com
    service: ssh://localhost:22
  - service: http_status:404
YAML
sudo nano /etc/cloudflared/config.yml        # paste the real UUID and hostname
sudo cp ~/.cloudflared/*.json /etc/cloudflared/

sudo cloudflared service install
sudo systemctl enable --now cloudflared
systemctl status cloudflared
cloudflared tunnel info ricardi
```

Healthy looks like `active (running)`, a QUIC primary protocol, and connectors
listed against edge locations.

## 5. Access, which is the actual lock

**The moment that hostname resolves, it is an SSH endpoint reachable from the
whole internet.** The tunnel routes around your home router; Cloudflare Access
is what replaces it. This step is not optional and it is not later.

Zero Trust → Access → Applications → **Add an application** → **Self-hosted**:

- Subdomain `ricardi`, your domain
- Policy: name it, Action **Allow**
- Include → **Emails** → your address

**Add a second email on a different domain.** Include rules are OR'd. The
default login method mails a one-time PIN, so if the address you listed is on
the same domain whose DNS you are editing, one bad MX change locks you out of
the only box you can reach through Access. A fallback on an unrelated domain
costs nothing.

If the login page offers no way in, enable **One-time PIN** under
Settings → Authentication → Login methods.

Note that Cloudflare asks for a payment method to activate a Zero Trust
organization even on the free tier. It is free for up to 50 users; the card is
a gate, not a charge. If you would rather not, Tailscale solves this exact
problem with no card, no DNS record and no policy — `tailscale up --ssh` on
both ends — and the two can coexist.

## 6. The client

```powershell
winget install --id Cloudflare.cloudflared
```

Reopen the terminal so it lands on `PATH`. Then `~/.ssh/config`:

```
Host ricardi-home
  HostName ricardi.example.com
  User <user>
  ProxyCommand cloudflared access ssh --hostname %h
```

```powershell
ssh ricardi-home
```

A browser opens, you authenticate, the token caches. If it hangs, run the auth
step alone to see what it is waiting for:

```powershell
cloudflared access login ricardi.example.com
```

## 7. Hardening, in this order

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ricardi-home "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Open a **fresh** terminal and confirm key auth works. Only then:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

Keep a console path in reserve — Chrome Remote Desktop to the host, or powering
the VM off and starting it interactively from an elevated Workstation. If
`cloudflared` itself fails to start, the tunnel cannot tell you so.

## 8. The only test that counts

Hard-reboot the host. **Do not log in.** Walk away. Then from somewhere else:

```powershell
ssh ricardi-home
```

If that lands, all three links held with no human present: host boot, autostart
service without a session, VM networking, and the tunnel dialling out. Nothing
short of a cold boot tests the service's logon account, and nothing short of
staying logged out tests the thing you are actually claiming.

Do this before any week-long soak. A host that silently fails to bring the VM
back after a Windows Update reboot costs you the whole week.

## When it goes wrong

| Symptom | Cause |
|---|---|
| "Failed to update AutoStart configuration" | Workstation not elevated. UAC drops the Administrators SID, leaving `Everyone:(R)`. |
| Autostart worked once, never again | Stale `.lck` directories beside the `.vmx` from a hard power-off. |
| `vmrun list` shows nothing but the VM is up | Started in another context; your logged-in shell cannot see it. Not evidence. |
| `malformed address '192.168.X.10/24'` | A placeholder reached the file. |
| `netplan apply` leaves the interface with no address | NetworkManager renderer didn't reload. `systemctl restart NetworkManager`. |
| "systemd-networkd is not running" from `netplan try` | Same cause. The rollback is not protecting you; verify by hand. |
| `reboot` refuses, "user session inhibited" | A desktop session holds an inhibitor. `systemctl reboot -i`. |
| "X is neither the ID nor the name of any of your tunnels" | `route dns` before `tunnel create`. |
| Tunnel healthy, SSH refuses | Check the Access policy exists and your email matches an Include rule. |
| `apt` or `docker pull` oddly slow | DNS answered AAAA-only and there is no routable IPv6. `echo 'Acquire::ForceIPv4 "true";' | sudo tee /etc/apt/apt.conf.d/99force-ipv4` |

## What will break later

Written down because each of these fails silently, months out, with no obvious
link back to this document.

- **The autostart service's logon password changes.** The service keeps
  starting and the VM stops appearing. Same failure shape as a dead
  credential: nothing is down, nothing answers.
- **The Access one-time PIN stops arriving.** Mail routing on the domain
  changes and you lose the only way in. This is what the second Include rule
  is for.
- **The DHCP pool or VMnet8 subnet changes** after a Workstation upgrade or a
  network editor reset, stranding the static address. `ping` the gateway first
  when the VM is up but unreachable.
- **A guest reboot that needs `-i`** will hang a script that reboots without
  it, as long as anything holds an inhibitor.

See [`running.md`](running.md) for the layer above this: the credential that
has to keep working once the box does, and the watch that reports it when it
doesn't.
