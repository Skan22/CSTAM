/* Lab only: inside `unshare --map-root-user` setgroups() is always denied, and keepalived calls it
 * unconditionally before running every script. Pretend it worked. */
#include <grp.h>
int setgroups(size_t n, const gid_t *list) { (void)n; (void)list; return 0; }
