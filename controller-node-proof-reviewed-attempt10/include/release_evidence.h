#ifndef U20_RELEASE_EVIDENCE_H
#define U20_RELEASE_EVIDENCE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifndef RELEASE_EVIDENCE_ENABLED
#define RELEASE_EVIDENCE_ENABLED 0
#endif

#define RE_EVENT_ID_BYTES 16U
#define RE_BOOT_ID_MAX 64U
#define RE_NODE_MAX 128U
#define RE_PEER_MAX 128U
#define RE_CGROUP_MAX 512U

typedef struct {
    uint64_t job_id;
    uint64_t allocation_id;
    uint64_t generation;
} re_allocation_key;

typedef enum {
    RE_UNKNOWN = 0,
    RE_RUNNING = 1,
    RE_RELEASED = 2
} re_state;

typedef enum {
    RE_EVENT_ALLOCATION_STARTED = 1,
    RE_EVENT_FINALIZATION_REQUESTED = 2,
    RE_EVENT_CLEANUP_ATTESTED = 3,
    RE_EVENT_NEGATIVE_EVIDENCE = 4,
    RE_EVENT_NODE_RESTART = 5,
    RE_EVENT_ADMIN_RECOVERY = 6
} re_event_kind;

typedef enum {
    RE_OK = 0,
    RE_NOOP_DUPLICATE = 1,
    RE_DISABLED = 2,
    RE_INVALID = -1,
    RE_UNAUTHORIZED = -2,
    RE_ORDERING = -3,
    RE_NOT_FOUND = -4,
    RE_IO = -5,
    RE_CORRUPT = -6,
    RE_UNKNOWN_RESULT = -7,
    RE_BUSY = -8,
    RE_UNINITIALIZED = -9
} re_result;

typedef struct {
    uint8_t event_id[RE_EVENT_ID_BYTES];
    re_event_kind kind;
    re_allocation_key allocation;
    char node[RE_NODE_MAX];
    char boot_id[RE_BOOT_ID_MAX];
    char authorized_peer[RE_PEER_MAX];
    uint64_t node_sequence;
    uint64_t node_monotonic_ns;
    char cgroup_path[RE_CGROUP_MAX];
    uint64_t cgroup_inode;
    uint64_t device_lease_token;
    uint64_t process_scan_before_token;
    uint64_t process_scan_after_token;
    uint32_t process_count_before;
    uint32_t process_count_after;
    bool process_inventory_complete;
    bool pid_starttime_checked;
    bool pid_reuse_checked;
    bool cgroup_identity_checked;
    bool cgroup_empty;
    bool cgroup_cleanup_completed;
    bool device_release_confirmed;
    bool negative_evidence;
    bool controller_partition_seen;
    bool admin_approved;
    const uint8_t *authenticated_bytes;
    size_t authenticated_bytes_len;
} re_event;

typedef struct {
    re_state state;
    bool release_allowed;
    bool usage_must_remain_held;
    uint64_t last_node_sequence;
    char boot_id[RE_BOOT_ID_MAX];
} re_observation;

/* The verifier must authenticate all event fields, including the opaque bytes. */
typedef bool (*re_event_verifier)(const re_event *event, void *context);

/* Administrative approval must be distinct from node evidence verification. */
typedef bool (*re_admin_verifier)(const re_event *event, void *context);

/* These callbacks must bind to an external trusted controller record. */
typedef bool (*re_anchor_verifier)(const uint8_t *anchor, size_t anchor_len,
                                   void *context);
typedef bool (*re_commit_verifier)(const re_allocation_key *allocation,
                                   const uint8_t event_id[RE_EVENT_ID_BYTES],
                                   uint64_t node_sequence, void *context);

typedef struct {
    re_event_verifier verify_event;
    re_admin_verifier verify_admin;
    re_anchor_verifier verify_anchor;
    re_commit_verifier verify_commit;
    void *verifier_context;
    const uint8_t *initialization_anchor;
    size_t initialization_anchor_len;
    bool prototype_enable;
} re_store_options;

typedef struct re_store re_store;

re_result re_store_initialize(const re_store_options *options,
                              const char *journal_path,
                              const uint8_t *anchor, size_t anchor_len);

re_result re_store_open(re_store **out, const re_store_options *options,
                        const char *journal_path);
void re_store_close(re_store *store);
re_result re_store_replay(re_store *store);
re_result re_store_apply(re_store *store, const re_event *event);
re_result re_store_observe(const re_store *store, const re_allocation_key *key,
                           re_observation *out);
re_result re_store_admission_gate(const re_store *store, const char *node);

#endif
