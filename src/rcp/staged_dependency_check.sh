# Report which programs this account cannot start. POSIX sh; runs under dash.
# Usage: sh -s -- <nonce> <name | /absolute/path | login:name>...
# Only the lines between the begin and end markers count; the caller ignores
# anything a login shell prints around them. A `login:` name is looked up in
# the interactive login shell that starts remote providers; its stdin is closed
# so it cannot read the rest of this script.
nonce=$1
shift
printf 'rcp-dependency-check begin %s\n' "$nonce"
os=$(uname -s 2>/dev/null)
printf 'os %s\n' "$os"
if [ "$os" = Linux ] && [ -r /etc/os-release ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      ID=*) key=id value=${line#ID=} ;;
      ID_LIKE=*) key=id_like value=${line#ID_LIKE=} ;;
      *) continue ;;
    esac
    value=${value#[\"\']}
    value=${value%[\"\']}
    printf '%s %s\n' "$key" "$value"
  done < /etc/os-release
fi
for name in "$@"; do
  case $name in
    /*) [ -x "$name" ] || printf 'missing %s\n' "$name" ;;
    login:*)
      bash -lic 'command -v "$1"' rcp "${name#login:}" </dev/null >/dev/null 2>&1 \
        || printf 'missing %s\n' "$name" ;;
    *) command -v "$name" >/dev/null 2>&1 || printf 'missing %s\n' "$name" ;;
  esac
done
printf 'rcp-dependency-check end %s\n' "$nonce"
