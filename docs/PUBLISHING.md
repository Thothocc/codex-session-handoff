# Publishing to GitHub / 发布到 GitHub

## 1. Create a repository on GitHub

Create an empty repository, for example:

```text
your-name/codex-session-handoff
```

Do not add a README, license, or `.gitignore` on GitHub if this local folder already has them.

## 2. Check for private generated files

Before committing:

```bash
find . -name "*.jsonl"
find . -name "CHATGPT_HANDOFF_INPUT*.md"
find . -name "SESSION_SELECTION*.md"
find . -name "codex_handoff*" -type d
```

Remove any generated packs or private session files.

## 3. Initialize Git and push

```bash
git init
git add .
git commit -m "Initial release: Codex session handoff packer"
git branch -M main
git remote add origin git@github.com:YOUR_NAME/codex-session-handoff.git
git push -u origin main
```

## SSH notes

If you do not have a private key matching your public key, generate a new key:

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
cat ~/.ssh/id_ed25519.pub
```

Add the public key to GitHub:

```text
GitHub → Settings → SSH and GPG keys → New SSH key
```

Test:

```bash
ssh -T git@github.com
```

## HTTPS alternative

```bash
git remote add origin https://github.com/YOUR_NAME/codex-session-handoff.git
git push -u origin main
```

For HTTPS, use a personal access token instead of your GitHub password.
