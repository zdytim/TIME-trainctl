#define _POSIX_C_SOURCE 200809L

#include "release_evidence.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define RE_JOURNAL_MAGIC 0x55323031U
#define RE_JOURNAL_VERSION 1U
#define RE_JOURNAL_INIT_KIND (UINT16_MAX - 1U)
#define RE_JOURNAL_PREPARE_KIND (UINT16_MAX - 2U)
#define RE_JOURNAL_COMMIT_KIND (UINT16_MAX - 3U)
#define RE_JOURNAL_FAULT_KIND (UINT16_MAX - 4U)
#define RE_MAX_RECORD (1024U * 1024U)
#define RE_MAX_AUTH_BYTES 8192U
#define RE_MAX_ANCHOR_BYTES 8192U
#define RE_MAX_IDEMPOTENCY 256U
#define RE_MAX_NODE_BOOT_TRACKERS 256U

typedef struct {
    re_allocation_key key;
    re_state state;
    char node[RE_NODE_MAX];
    char boot_id[RE_BOOT_ID_MAX];
    char authorized_peer[RE_PEER_MAX];
    char cgroup_path[RE_CGROUP_MAX];
    uint64_t cgroup_inode;
    uint64_t device_lease_token;
    uint64_t last_sequence;
    uint8_t last_event_id[RE_EVENT_ID_BYTES];
    uint8_t *last_authenticated_bytes;
    size_t last_authenticated_bytes_len;
    bool has_start;
    bool usage_must_remain_held;
    bool finalization_requested;
    bool requires_admin_recovery;
    bool negative_evidence_seen;
    bool restart_seen;
} re_generation_state;

typedef struct {
    bool used;
    char node[RE_NODE_MAX];
    char boot_id[RE_BOOT_ID_MAX];
    uint64_t last_sequence;
    bool blocked;
} re_node_boot_sequence;

typedef struct {
    bool used;
    re_event event;
    uint32_t authenticated_bytes_len;
    uint8_t authenticated_bytes[RE_MAX_AUTH_BYTES];
} re_event_index_entry;

typedef struct {
    re_allocation_key key;
    uint8_t event_id[RE_EVENT_ID_BYTES];
    uint32_t reason;
    uint64_t durable_record_count;
    uint64_t node_sequence;
    uint8_t previous_chain_head[RE_CHAIN_BYTES];
    uint8_t record_chain_head[RE_CHAIN_BYTES];
    char node[RE_NODE_MAX];
    char boot_id[RE_BOOT_ID_MAX];
    bool block_node;
    char secondary_node[RE_NODE_MAX];
    char secondary_boot_id[RE_BOOT_ID_MAX];
    re_allocation_key secondary_key;
    bool block_secondary_allocation;
    bool block_secondary;
} re_fault_record;

typedef struct {
    uint8_t chain_head[RE_CHAIN_BYTES];
    uint32_t anchor_len;
    uint8_t anchor[RE_MAX_ANCHOR_BYTES];
} re_init_record;

typedef struct {
    re_allocation_key key;
    uint8_t event_id[RE_EVENT_ID_BYTES];
    char node[RE_NODE_MAX];
    char boot_id[RE_BOOT_ID_MAX];
    uint64_t node_sequence;
    uint64_t durable_record_count;
    uint8_t previous_chain_head[RE_CHAIN_BYTES];
    uint8_t record_chain_head[RE_CHAIN_BYTES];
} re_commit_record;

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t kind;
    uint32_t body_len;
} re_record_header;

typedef struct {
    re_event event;
    uint32_t authenticated_bytes_len;
} re_disk_event;

struct re_store {
    int fd;
    pthread_mutex_t mutex;
    bool mutex_ready;
    bool recovered;
    bool recovery_failed;
    bool initialized;
    re_event_verifier verify_event;
    re_admin_verifier verify_admin;
    re_anchor_verifier verify_anchor;
    re_commit_verifier verify_commit;
    re_recovery_verifier verify_recovery;
    void *verifier_context;
    bool prototype_enable;
    size_t initialization_anchor_len;
    uint8_t initialization_anchor[RE_MAX_ANCHOR_BYTES];
    uint8_t initialization_chain_head[RE_CHAIN_BYTES];
    uint8_t chain_head[RE_CHAIN_BYTES];
    uint64_t durable_record_count;
    re_generation_state *states;
    size_t state_count;
    size_t state_capacity;
    re_node_boot_sequence *node_boot_sequences;
    size_t node_boot_sequence_count;
    re_event_index_entry *event_index;
    size_t event_index_count;
};

static bool bounded_string(const char *value, size_t limit)
{
    return value != NULL && memchr(value, '\0', limit) != NULL && value[0] != '\0';
}

static bool terminated_field(const char *value, size_t limit)
{
    return value != NULL && memchr(value, '\0', limit) != NULL;
}

static bool field_equal(const char *a, size_t a_limit, const char *b,
                        size_t b_limit)
{
    return terminated_field(a, a_limit) && terminated_field(b, b_limit) &&
           strcmp(a, b) == 0;
}

static bool nonzero_event_id(const uint8_t event_id[RE_EVENT_ID_BYTES])
{
    size_t i;

    for (i = 0; i < RE_EVENT_ID_BYTES; ++i) {
        if (event_id[i] != 0U) {
            return true;
        }
    }
    return false;
}

static bool nonzero_chain_head(const uint8_t chain_head[RE_CHAIN_BYTES])
{
    size_t i;

    for (i = 0; i < RE_CHAIN_BYTES; ++i) {
        if (chain_head[i] != 0U) {
            return true;
        }
    }
    return false;
}

static bool key_equal(const re_allocation_key *a, const re_allocation_key *b)
{
    return a->job_id == b->job_id && a->allocation_id == b->allocation_id &&
           a->generation == b->generation;
}

static re_generation_state *find_state(re_store *store,
                                       const re_allocation_key *key)
{
    size_t i;

    for (i = 0; i < store->state_count; ++i) {
        if (key_equal(&store->states[i].key, key)) {
            return &store->states[i];
        }
    }
    return NULL;
}

static const re_generation_state *find_state_const(
    const re_store *store, const re_allocation_key *key)
{
    size_t i;

    for (i = 0; i < store->state_count; ++i) {
        if (key_equal(&store->states[i].key, key)) {
            return &store->states[i];
        }
    }
    return NULL;
}

static bool secure_directory_stat(const struct stat *st)
{
    uid_t owner = geteuid();

    return S_ISDIR(st->st_mode) &&
           (st->st_uid == owner || st->st_uid == 0U) &&
           (st->st_mode & (S_IWGRP | S_IWOTH)) == 0U;
}

static int open_secure_parent(const char *journal_path,
                              char leaf[NAME_MAX + 1U])
{
    char parent[PATH_MAX];
    const char *last_slash;
    size_t parent_length;
    size_t position;
    int directory_fd;

    if (journal_path == NULL || journal_path[0] != '/' ||
        strlen(journal_path) >= sizeof(parent)) {
        return -1;
    }
    last_slash = strrchr(journal_path, '/');
    if (last_slash == NULL || last_slash[1] == '\0' ||
        strcmp(last_slash + 1, ".") == 0 ||
        strcmp(last_slash + 1, "..") == 0 ||
        strlen(last_slash + 1) > NAME_MAX) {
        return -1;
    }
    (void)snprintf(leaf, NAME_MAX + 1U, "%s", last_slash + 1);
    parent_length = (size_t)(last_slash - journal_path);
    if (parent_length == 0U) {
        parent_length = 1U;
    }
    if (parent_length >= sizeof(parent)) {
        return -1;
    }
    memcpy(parent, journal_path, parent_length);
    parent[parent_length] = '\0';

    directory_fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory_fd < 0) {
        return -1;
    }
    position = 1U;
    while (position < parent_length) {
        char component[NAME_MAX + 1U];
        size_t start = position;
        size_t length;
        int next_fd;
        struct stat st;

        while (position < parent_length && parent[position] == '/') {
            ++position;
        }
        if (position == parent_length) {
            break;
        }
        while (position < parent_length && parent[position] != '/') {
            ++position;
        }
        length = position - start;
        if (length == 0U || length > NAME_MAX ||
            (length == 1U && parent[start] == '.') ||
            (length == 2U && parent[start] == '.' &&
             parent[start + 1U] == '.')) {
            (void)close(directory_fd);
            return -1;
        }
        memcpy(component, parent + start, length);
        component[length] = '\0';
        next_fd = openat(directory_fd, component,
                         O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (next_fd < 0 || fstat(next_fd, &st) != 0 ||
            !secure_directory_stat(&st)) {
            if (next_fd >= 0) {
                (void)close(next_fd);
            }
            (void)close(directory_fd);
            return -1;
        }
        (void)close(directory_fd);
        directory_fd = next_fd;
    }
    return directory_fd;
}

static int open_locked_journal(const char *journal_path, bool create)
{
    char leaf[NAME_MAX + 1U];
    struct stat st;
    int parent_fd;
    int journal_fd;

    parent_fd = open_secure_parent(journal_path, leaf);
    if (parent_fd < 0) {
        return -1;
    }
    if (create) {
        journal_fd = openat(parent_fd, leaf,
                            O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW |
                                O_CLOEXEC,
                            S_IRUSR | S_IWUSR);
    } else {
        journal_fd = openat(parent_fd, leaf,
                            O_RDWR | O_NOFOLLOW | O_CLOEXEC);
    }
    if (journal_fd < 0 || fstat(journal_fd, &st) != 0 ||
        !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0777U) != 0600U ||
        flock(journal_fd, LOCK_EX | LOCK_NB) != 0 || fsync(parent_fd) != 0) {
        if (journal_fd >= 0) {
            (void)close(journal_fd);
        }
        (void)close(parent_fd);
        return -1;
    }
    (void)close(parent_fd);
    return journal_fd;
}

static bool write_full(int fd, const void *buffer, size_t length)
{
    const uint8_t *bytes = buffer;
    size_t written = 0U;

    while (written < length) {
        ssize_t result = write(fd, bytes + written, length - written);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            return false;
        }
        written += (size_t)result;
    }
    return true;
}

static bool read_full(int fd, void *buffer, size_t length)
{
    uint8_t *bytes = buffer;
    size_t read_count = 0U;

    while (read_count < length) {
        ssize_t result = read(fd, bytes + read_count, length - read_count);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            return false;
        }
        read_count += (size_t)result;
    }
    return true;
}

static bool event_shape_valid(const re_event *event)
{
    if (event == NULL || !nonzero_event_id(event->event_id) ||
        !nonzero_chain_head(event->previous_chain_head) ||
        !nonzero_chain_head(event->record_chain_head) ||
        event->kind < RE_EVENT_ALLOCATION_STARTED ||
        event->kind > RE_EVENT_ADMIN_RECOVERY || event->allocation.job_id == 0U ||
        event->allocation.allocation_id == 0U ||
        event->allocation.generation == 0U ||
        !bounded_string(event->node, sizeof(event->node)) ||
        !bounded_string(event->boot_id, sizeof(event->boot_id)) ||
        !bounded_string(event->authorized_peer, sizeof(event->authorized_peer)) ||
        !terminated_field(event->cgroup_path, sizeof(event->cgroup_path)) ||
        event->node_sequence == 0U || event->authenticated_bytes == NULL ||
        event->authenticated_bytes_len == 0U ||
        event->authenticated_bytes_len > RE_MAX_AUTH_BYTES) {
        return false;
    }
    if ((event->kind == RE_EVENT_ALLOCATION_STARTED ||
         event->kind == RE_EVENT_CLEANUP_ATTESTED ||
         event->kind == RE_EVENT_ADMIN_RECOVERY) &&
        (event->cgroup_path[0] == '\0' ||
         event->cgroup_inode == 0U)) {
        return false;
    }
    if (event->kind == RE_EVENT_ALLOCATION_STARTED &&
        (event->negative_evidence || event->controller_partition_seen ||
         event->admin_approved)) {
        return false;
    }
    if (event->kind == RE_EVENT_NEGATIVE_EVIDENCE &&
        !event->negative_evidence && !event->controller_partition_seen) {
        return false;
    }
    if (event->kind == RE_EVENT_ALLOCATION_STARTED &&
        (event->device_lease_token == 0U || event->negative_evidence ||
         event->controller_partition_seen || event->admin_approved)) {
        return false;
    }
    return true;
}

static bool positive_cleanup(const re_event *event)
{
    return event->device_lease_token != 0U &&
           event->process_scan_before_token != 0U &&
           event->process_scan_after_token != 0U &&
           event->process_count_after == 0U &&
           event->process_inventory_complete &&
           event->pid_starttime_checked && event->pid_reuse_checked &&
           event->cgroup_identity_checked && event->cgroup_empty &&
           event->cgroup_cleanup_completed &&
           event->device_release_confirmed && !event->negative_evidence &&
           !event->controller_partition_seen;
}

static bool event_metadata_equal(const re_event *a, const re_event *b)
{
    return memcmp(a->event_id, b->event_id, RE_EVENT_ID_BYTES) == 0 &&
           memcmp(a->previous_chain_head, b->previous_chain_head,
                  RE_CHAIN_BYTES) == 0 &&
           memcmp(a->record_chain_head, b->record_chain_head,
                  RE_CHAIN_BYTES) == 0 &&
           a->kind == b->kind && key_equal(&a->allocation, &b->allocation) &&
           field_equal(a->node, sizeof(a->node), b->node, sizeof(b->node)) &&
           field_equal(a->boot_id, sizeof(a->boot_id), b->boot_id,
                       sizeof(b->boot_id)) &&
           field_equal(a->authorized_peer, sizeof(a->authorized_peer),
                       b->authorized_peer, sizeof(b->authorized_peer)) &&
           a->node_sequence == b->node_sequence &&
           a->node_monotonic_ns == b->node_monotonic_ns &&
           field_equal(a->cgroup_path, sizeof(a->cgroup_path), b->cgroup_path,
                       sizeof(b->cgroup_path)) &&
           a->cgroup_inode == b->cgroup_inode &&
           a->device_lease_token == b->device_lease_token &&
           a->process_scan_before_token == b->process_scan_before_token &&
           a->process_scan_after_token == b->process_scan_after_token &&
           a->process_count_before == b->process_count_before &&
           a->process_count_after == b->process_count_after &&
           a->process_inventory_complete == b->process_inventory_complete &&
           a->pid_starttime_checked == b->pid_starttime_checked &&
           a->pid_reuse_checked == b->pid_reuse_checked &&
           a->cgroup_identity_checked == b->cgroup_identity_checked &&
           a->cgroup_empty == b->cgroup_empty &&
           a->cgroup_cleanup_completed == b->cgroup_cleanup_completed &&
           a->device_release_confirmed == b->device_release_confirmed &&
           a->negative_evidence == b->negative_evidence &&
           a->controller_partition_seen == b->controller_partition_seen &&
           a->admin_approved == b->admin_approved &&
           a->authenticated_bytes_len == b->authenticated_bytes_len &&
           memcmp(a->authenticated_bytes, b->authenticated_bytes,
                  a->authenticated_bytes_len) == 0;
}

static bool append_initialization_record(int fd, const uint8_t *anchor,
                                         size_t anchor_len,
                                         const uint8_t chain_head[RE_CHAIN_BYTES])
{
    re_record_header header;
    re_init_record init;

    memset(&init, 0, sizeof(init));
    memcpy(init.chain_head, chain_head, RE_CHAIN_BYTES);
    init.anchor_len = (uint32_t)anchor_len;
    memcpy(init.anchor, anchor, anchor_len);
    header.magic = RE_JOURNAL_MAGIC;
    header.version = RE_JOURNAL_VERSION;
    header.kind = RE_JOURNAL_INIT_KIND;
    header.body_len = (uint32_t)(sizeof(init.chain_head) +
                                 sizeof(init.anchor_len) + anchor_len);
    return write_full(fd, &header, sizeof(header)) &&
           write_full(fd, init.chain_head, sizeof(init.chain_head)) &&
           write_full(fd, &init.anchor_len, sizeof(init.anchor_len)) &&
           write_full(fd, init.anchor, anchor_len) &&
           fsync(fd) == 0;
}

static bool append_event_record(re_store *store, const re_event *event)
{
    re_record_header header;
    re_disk_event disk_event;

    header.magic = RE_JOURNAL_MAGIC;
    header.version = RE_JOURNAL_VERSION;
    header.kind = RE_JOURNAL_PREPARE_KIND;
    header.body_len = (uint32_t)(sizeof(disk_event) +
                                 event->authenticated_bytes_len);
    memset(&disk_event, 0, sizeof(disk_event));
    disk_event.event = *event;
    disk_event.event.authenticated_bytes = NULL;
    disk_event.authenticated_bytes_len =
        (uint32_t)event->authenticated_bytes_len;
    if (header.body_len > RE_MAX_RECORD ||
        !write_full(store->fd, &header, sizeof(header)) ||
        !write_full(store->fd, &disk_event, sizeof(disk_event)) ||
        !write_full(store->fd, event->authenticated_bytes,
                    event->authenticated_bytes_len) ||
        fsync(store->fd) != 0) {
        return false;
    }
    return true;
}

static bool append_commit_record(re_store *store, const re_event *event)
{
    re_commit_record commit;
    re_record_header header;

    memset(&commit, 0, sizeof(commit));
    commit.key = event->allocation;
    memcpy(commit.event_id, event->event_id, RE_EVENT_ID_BYTES);
    (void)snprintf(commit.node, sizeof(commit.node), "%s", event->node);
    (void)snprintf(commit.boot_id, sizeof(commit.boot_id), "%s", event->boot_id);
    commit.node_sequence = event->node_sequence;
    commit.durable_record_count = store->durable_record_count + 1U;
    memcpy(commit.previous_chain_head, event->previous_chain_head,
           RE_CHAIN_BYTES);
    memcpy(commit.record_chain_head, event->record_chain_head, RE_CHAIN_BYTES);
    header.magic = RE_JOURNAL_MAGIC;
    header.version = RE_JOURNAL_VERSION;
    header.kind = RE_JOURNAL_COMMIT_KIND;
    header.body_len = sizeof(commit);
    return write_full(store->fd, &header, sizeof(header)) &&
           write_full(store->fd, &commit, sizeof(commit)) && fsync(store->fd) == 0;
}

static bool append_fault_record(re_store *store, const re_fault_record *fault)
{
    re_record_header header;

    header.magic = RE_JOURNAL_MAGIC;
    header.version = RE_JOURNAL_VERSION;
    header.kind = RE_JOURNAL_FAULT_KIND;
    header.body_len = sizeof(*fault);
    return write_full(store->fd, &header, sizeof(header)) &&
           write_full(store->fd, fault, sizeof(*fault)) &&
           fsync(store->fd) == 0;
}

static void force_unknown(re_generation_state *state)
{
    if (state != NULL) {
        state->state = RE_UNKNOWN;
        state->usage_must_remain_held = true;
        state->requires_admin_recovery = true;
    }
}

static void force_all_unknown(re_store *store)
{
    size_t i;

    for (i = 0; i < store->state_count; ++i) {
        force_unknown(&store->states[i]);
    }
}

static bool ensure_state_capacity(re_store *store)
{
    size_t capacity;
    re_generation_state *states;

    if (store->state_count < store->state_capacity) {
        return true;
    }
    capacity = store->state_capacity == 0U ? 8U : store->state_capacity * 2U;
    states = realloc(store->states, capacity * sizeof(*states));
    if (states == NULL) {
        return false;
    }
    store->states = states;
    store->state_capacity = capacity;
    return true;
}

static void initialize_unknown_state(re_generation_state *state,
                                     const re_event *event)
{
    memset(state, 0, sizeof(*state));
    state->key = event->allocation;
    state->state = RE_UNKNOWN;
    state->usage_must_remain_held = true;
    state->requires_admin_recovery = true;
    (void)snprintf(state->node, sizeof(state->node), "%s", event->node);
    (void)snprintf(state->boot_id, sizeof(state->boot_id), "%s", event->boot_id);
    (void)snprintf(state->authorized_peer, sizeof(state->authorized_peer),
                   "%s", event->authorized_peer);
}

static re_node_boot_sequence *find_sequence(re_store *store,
                                            const re_event *event)
{
    size_t i;

    for (i = 0; i < store->node_boot_sequence_count; ++i) {
        re_node_boot_sequence *entry = &store->node_boot_sequences[i];
        if (entry->used &&
            field_equal(entry->node, sizeof(entry->node), event->node,
                        sizeof(event->node)) &&
            field_equal(entry->boot_id, sizeof(entry->boot_id),
                        event->boot_id, sizeof(event->boot_id))) {
            return entry;
        }
    }
    return NULL;
}

static re_node_boot_sequence *find_sequence_identity(
    re_store *store, const char *node, const char *boot_id)
{
    size_t i;

    for (i = 0; i < store->node_boot_sequence_count; ++i) {
        re_node_boot_sequence *entry = &store->node_boot_sequences[i];
        if (entry->used && field_equal(entry->node, sizeof(entry->node), node,
                                       RE_NODE_MAX) &&
            field_equal(entry->boot_id, sizeof(entry->boot_id), boot_id,
                        RE_BOOT_ID_MAX)) {
            return entry;
        }
    }
    return NULL;
}

static bool block_node_boot(re_store *store, const char *node,
                            const char *boot_id)
{
    re_node_boot_sequence *entry = find_sequence_identity(store, node, boot_id);

    if (entry == NULL) {
        if (store->node_boot_sequence_count >= RE_MAX_NODE_BOOT_TRACKERS) {
            return false;
        }
        entry = &store->node_boot_sequences[store->node_boot_sequence_count++];
        memset(entry, 0, sizeof(*entry));
        entry->used = true;
        (void)snprintf(entry->node, sizeof(entry->node), "%s", node);
        (void)snprintf(entry->boot_id, sizeof(entry->boot_id), "%s", boot_id);
    }
    entry->blocked = true;
    return true;
}

static bool node_is_blocked(const re_store *store, const char *node)
{
    size_t i;

    for (i = 0; i < store->node_boot_sequence_count; ++i) {
        const re_node_boot_sequence *entry = &store->node_boot_sequences[i];
        if (entry->used && entry->blocked &&
            field_equal(entry->node, sizeof(entry->node), node, RE_NODE_MAX)) {
            return true;
        }
    }
    return false;
}

static bool sequence_slot_available(const re_store *store,
                                    const re_event *event)
{
    return find_sequence((re_store *)store, event) != NULL ||
           store->node_boot_sequence_count < RE_MAX_NODE_BOOT_TRACKERS;
}

static bool record_sequence(re_store *store, const re_event *event,
                            bool block_node)
{
    re_node_boot_sequence *entry = find_sequence(store, event);

    if (entry == NULL) {
        if (store->node_boot_sequence_count >= RE_MAX_NODE_BOOT_TRACKERS) {
            return false;
        }
        entry = &store->node_boot_sequences[store->node_boot_sequence_count++];
        memset(entry, 0, sizeof(*entry));
        entry->used = true;
        (void)snprintf(entry->node, sizeof(entry->node), "%s", event->node);
        (void)snprintf(entry->boot_id, sizeof(entry->boot_id), "%s",
                       event->boot_id);
    }
    entry->last_sequence = event->node_sequence;
    return !block_node || block_node_boot(store, event->node, event->boot_id);
}

static re_event_index_entry *find_event_index(
    re_store *store, const uint8_t event_id[RE_EVENT_ID_BYTES])
{
    size_t i;

    for (i = 0; i < store->event_index_count; ++i) {
        if (store->event_index[i].used &&
            memcmp(store->event_index[i].event.event_id, event_id,
                   RE_EVENT_ID_BYTES) == 0) {
            return &store->event_index[i];
        }
    }
    return NULL;
}

static bool remember_event(re_store *store, const re_event *event)
{
    re_event_index_entry *entry;

    if (store->event_index_count >= RE_MAX_IDEMPOTENCY ||
        event->authenticated_bytes_len > RE_MAX_AUTH_BYTES) {
        return false;
    }
    entry = &store->event_index[store->event_index_count++];
    memset(entry, 0, sizeof(*entry));
    entry->used = true;
    entry->event = *event;
    entry->event.authenticated_bytes = entry->authenticated_bytes;
    entry->authenticated_bytes_len =
        (uint32_t)event->authenticated_bytes_len;
    memcpy(entry->authenticated_bytes, event->authenticated_bytes,
           event->authenticated_bytes_len);
    return true;
}

static void commit_last_event(re_generation_state *state,
                              const re_event *event, uint8_t *auth_copy)
{
    free(state->last_authenticated_bytes);
    state->last_authenticated_bytes = auth_copy;
    state->last_authenticated_bytes_len = event->authenticated_bytes_len;
    memcpy(state->last_event_id, event->event_id, RE_EVENT_ID_BYTES);
}

static re_result validate_global_sequence(const re_store *store,
                                          const re_event *event)
{
    re_node_boot_sequence *entry = find_sequence((re_store *)store, event);

    if (entry != NULL && event->node_sequence <= entry->last_sequence) {
        return RE_ORDERING;
    }
    if (entry == NULL && !sequence_slot_available(store, event)) {
        return RE_BUSY;
    }
    return RE_OK;
}

static re_result transition_state(re_store *store, const re_event *event,
                                  re_generation_state *state,
                                  bool has_state)
{
    bool recovery_ok = event->kind == RE_EVENT_ADMIN_RECOVERY &&
                       event->admin_approved && store->verify_admin != NULL &&
                       store->verify_admin(event, store->verifier_context);
    if (event->kind == RE_EVENT_FINALIZATION_REQUESTED &&
        (event->negative_evidence || event->controller_partition_seen ||
         event->admin_approved)) {
        if (!has_state) {
            initialize_unknown_state(state, event);
        } else {
            force_unknown(state);
        }
        state->finalization_requested = true;
        return RE_UNKNOWN_RESULT;
    }
    if (event->kind == RE_EVENT_NODE_RESTART) {
        if (has_state && !field_equal(state->node, sizeof(state->node),
                                      event->node, sizeof(event->node))) {
            force_unknown(state);
            return RE_UNKNOWN_RESULT;
        }
        if (!has_state) {
            initialize_unknown_state(state, event);
        }
        state->restart_seen = true;
        force_unknown(state);
        return RE_UNKNOWN_RESULT;
    }
    if (event->kind == RE_EVENT_NEGATIVE_EVIDENCE) {
        if (!has_state) {
            initialize_unknown_state(state, event);
        }
        state->negative_evidence_seen = true;
        force_unknown(state);
        return RE_UNKNOWN_RESULT;
    }
    if (!has_state) {
        if (event->kind == RE_EVENT_ALLOCATION_STARTED) {
            memset(state, 0, sizeof(*state));
            state->key = event->allocation;
            state->state = RE_RUNNING;
            state->usage_must_remain_held = true;
            state->has_start = true;
            (void)snprintf(state->node, sizeof(state->node), "%s", event->node);
            (void)snprintf(state->boot_id, sizeof(state->boot_id), "%s",
                           event->boot_id);
            (void)snprintf(state->authorized_peer,
                           sizeof(state->authorized_peer), "%s",
                           event->authorized_peer);
            (void)snprintf(state->cgroup_path, sizeof(state->cgroup_path),
                           "%s", event->cgroup_path);
            state->cgroup_inode = event->cgroup_inode;
            state->device_lease_token = event->device_lease_token;
            return RE_OK;
        }
        initialize_unknown_state(state, event);
        return RE_UNKNOWN_RESULT;
    }
    if (event->kind == RE_EVENT_ALLOCATION_STARTED ||
        (state->requires_admin_recovery &&
         event->kind != RE_EVENT_ADMIN_RECOVERY)) {
        force_unknown(state);
        return RE_UNKNOWN_RESULT;
    }
    if (!field_equal(state->node, sizeof(state->node), event->node,
                     sizeof(event->node)) ||
        !field_equal(state->boot_id, sizeof(state->boot_id), event->boot_id,
                     sizeof(event->boot_id)) ||
        !field_equal(state->authorized_peer, sizeof(state->authorized_peer),
                     event->authorized_peer, sizeof(event->authorized_peer))) {
        force_unknown(state);
        return RE_UNKNOWN_RESULT;
    }
    if (event->kind == RE_EVENT_CLEANUP_ATTESTED &&
        node_is_blocked(store, event->node)) {
        force_unknown(state);
        return RE_UNKNOWN_RESULT;
    }
    if (event->kind == RE_EVENT_CLEANUP_ATTESTED ||
        event->kind == RE_EVENT_ADMIN_RECOVERY) {
        bool cgroup_matches =
            field_equal(state->cgroup_path, sizeof(state->cgroup_path),
                        event->cgroup_path, sizeof(event->cgroup_path)) &&
            state->cgroup_inode == event->cgroup_inode &&
            state->device_lease_token == event->device_lease_token;
        if (!positive_cleanup(event) || !cgroup_matches ||
            (event->kind == RE_EVENT_ADMIN_RECOVERY && !recovery_ok)) {
            force_unknown(state);
            return RE_UNKNOWN_RESULT;
        }
        state->state = RE_RELEASED;
        state->usage_must_remain_held = false;
        state->requires_admin_recovery = false;
        return RE_OK;
    }
    if (event->kind == RE_EVENT_FINALIZATION_REQUESTED) {
        state->finalization_requested = true;
        state->state = RE_UNKNOWN;
        state->usage_must_remain_held = true;
        return RE_OK;
    }
    force_unknown(state);
    return RE_UNKNOWN_RESULT;
}

static bool mark_fault_state(re_store *store, const re_fault_record *fault)
{
    re_generation_state *state = find_state(store, &fault->key);

    if (state == NULL) {
        if (!ensure_state_capacity(store)) {
            return false;
        }
        state = &store->states[store->state_count++];
        memset(state, 0, sizeof(*state));
        state->key = fault->key;
    }
    force_unknown(state);
    if (!nonzero_event_id(fault->event_id) || fault->key.job_id == 0U ||
        fault->key.allocation_id == 0U || fault->key.generation == 0U) {
        return false;
    }
    if (fault->block_secondary_allocation) {
        re_generation_state *secondary = find_state(store, &fault->secondary_key);
        if (fault->secondary_key.job_id == 0U ||
            fault->secondary_key.allocation_id == 0U ||
            fault->secondary_key.generation == 0U) {
            return false;
        }
        if (secondary == NULL) {
            if (!ensure_state_capacity(store)) {
                return false;
            }
            secondary = &store->states[store->state_count++];
            memset(secondary, 0, sizeof(*secondary));
            secondary->key = fault->secondary_key;
        }
        force_unknown(secondary);
    }
    if (fault->block_node &&
        (!bounded_string(fault->node, sizeof(fault->node)) ||
         !bounded_string(fault->boot_id, sizeof(fault->boot_id)) ||
         !block_node_boot(store, fault->node, fault->boot_id))) {
        return false;
    }
    if (fault->block_secondary &&
        (!bounded_string(fault->secondary_node, sizeof(fault->secondary_node)) ||
         !bounded_string(fault->secondary_boot_id,
                         sizeof(fault->secondary_boot_id)) ||
         !block_node_boot(store, fault->secondary_node,
                          fault->secondary_boot_id))) {
        return false;
    }
    return true;
}

static re_result durable_fault(re_store *store, const re_event *event,
                               uint32_t reason, bool persist)
{
    re_fault_record fault;

    if (!event_shape_valid(event) ||
        memcmp(event->previous_chain_head, store->chain_head,
               RE_CHAIN_BYTES) != 0) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_CORRUPT;
    }
    memset(&fault, 0, sizeof(fault));
    fault.key = event->allocation;
    memcpy(fault.event_id, event->event_id, RE_EVENT_ID_BYTES);
    fault.reason = reason;
    fault.durable_record_count = store->durable_record_count + 1U;
    fault.node_sequence = event->node_sequence;
    memcpy(fault.previous_chain_head, event->previous_chain_head,
           RE_CHAIN_BYTES);
    memcpy(fault.record_chain_head, event->record_chain_head, RE_CHAIN_BYTES);
    if (bounded_string(event->node, sizeof(event->node)) &&
        bounded_string(event->boot_id, sizeof(event->boot_id))) {
        (void)snprintf(fault.node, sizeof(fault.node), "%s", event->node);
        (void)snprintf(fault.boot_id, sizeof(fault.boot_id), "%s",
                       event->boot_id);
        fault.block_node = true;
    }
    if (store->verify_commit == NULL ||
        !store->verify_commit(&fault.key, fault.event_id, fault.node_sequence,
                              fault.durable_record_count,
                              fault.previous_chain_head,
                              fault.record_chain_head,
                              true,
                              store->verifier_context)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_UNAUTHORIZED;
    }
    if (persist && !append_fault_record(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    if (!mark_fault_state(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    memcpy(store->chain_head, fault.record_chain_head, RE_CHAIN_BYTES);
    ++store->durable_record_count;
    return RE_UNKNOWN_RESULT;
}

static re_result durable_identity_fault(re_store *store,
                                        const re_event *event,
                                        const re_generation_state *trusted_state,
                                        bool persist)
{
    re_fault_record fault;

    memset(&fault, 0, sizeof(fault));
    fault.key = event->allocation;
    memcpy(fault.event_id, event->event_id, RE_EVENT_ID_BYTES);
    fault.reason = 9U;
    fault.durable_record_count = store->durable_record_count + 1U;
    fault.node_sequence = event->node_sequence;
    memcpy(fault.previous_chain_head, event->previous_chain_head,
           RE_CHAIN_BYTES);
    memcpy(fault.record_chain_head, event->record_chain_head, RE_CHAIN_BYTES);
    (void)snprintf(fault.node, sizeof(fault.node), "%s", trusted_state->node);
    (void)snprintf(fault.boot_id, sizeof(fault.boot_id), "%s",
                   trusted_state->boot_id);
    fault.block_node = true;
    (void)snprintf(fault.secondary_node, sizeof(fault.secondary_node), "%s",
                   event->node);
    (void)snprintf(fault.secondary_boot_id,
                   sizeof(fault.secondary_boot_id), "%s", event->boot_id);
    fault.block_secondary = true;
    if (store->verify_commit == NULL ||
        !store->verify_commit(&fault.key, fault.event_id, fault.node_sequence,
                              fault.durable_record_count,
                              fault.previous_chain_head,
                              fault.record_chain_head,
                              true,
                              store->verifier_context)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_UNAUTHORIZED;
    }
    if (persist && !append_fault_record(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    if (!mark_fault_state(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    memcpy(store->chain_head, fault.record_chain_head, RE_CHAIN_BYTES);
    ++store->durable_record_count;
    return RE_UNKNOWN_RESULT;
}

static re_result durable_event_conflict_fault(re_store *store,
                                              const re_event *event,
                                              const re_event *old_event,
                                              bool persist)
{
    re_fault_record fault;

    if (!event_shape_valid(event) ||
        memcmp(event->previous_chain_head, store->chain_head,
               RE_CHAIN_BYTES) != 0) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_CORRUPT;
    }
    memset(&fault, 0, sizeof(fault));
    fault.key = event->allocation;
    memcpy(fault.event_id, event->event_id, RE_EVENT_ID_BYTES);
    fault.reason = 3U;
    fault.durable_record_count = store->durable_record_count + 1U;
    fault.node_sequence = event->node_sequence;
    memcpy(fault.previous_chain_head, event->previous_chain_head,
           RE_CHAIN_BYTES);
    memcpy(fault.record_chain_head, event->record_chain_head, RE_CHAIN_BYTES);
    fault.secondary_key = old_event->allocation;
    fault.block_secondary_allocation = true;
    (void)snprintf(fault.node, sizeof(fault.node), "%s", event->node);
    (void)snprintf(fault.boot_id, sizeof(fault.boot_id), "%s", event->boot_id);
    fault.block_node = true;
    (void)snprintf(fault.secondary_node, sizeof(fault.secondary_node), "%s",
                   old_event->node);
    (void)snprintf(fault.secondary_boot_id,
                   sizeof(fault.secondary_boot_id), "%s", old_event->boot_id);
    fault.block_secondary = true;
    if (store->verify_commit == NULL ||
        !store->verify_commit(&fault.key, fault.event_id, fault.node_sequence,
                              fault.durable_record_count,
                              fault.previous_chain_head,
                              fault.record_chain_head,
                              true,
                              store->verifier_context)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_UNAUTHORIZED;
    }
    if (persist && !append_fault_record(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    if (!mark_fault_state(store, &fault)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    memcpy(store->chain_head, fault.record_chain_head, RE_CHAIN_BYTES);
    ++store->durable_record_count;
    return RE_UNKNOWN_RESULT;
}

static re_result apply_event_locked(re_store *store, const re_event *event,
                                    bool persist)
{
    re_event_index_entry *old_event;
    re_generation_state *current;
    re_generation_state staged;
    uint8_t *auth_copy;
    re_result result;
    bool has_state;

    if (!event_shape_valid(event)) {
        current = event != NULL ? find_state(store, &event->allocation) : NULL;
        if (persist && event != NULL) {
            (void)durable_fault(store, event, 1U, true);
        } else {
            force_unknown(current);
        }
        return RE_UNAUTHORIZED;
    }
    if (store->verify_event == NULL ||
        !store->verify_event(event, store->verifier_context)) {
        current = find_state(store, &event->allocation);
        if (persist) {
            return durable_fault(store, event, 2U, true);
        }
        force_unknown(current);
        return RE_UNAUTHORIZED;
    }
    old_event = find_event_index(store, event->event_id);
    if (old_event != NULL) {
        if (event_metadata_equal(&old_event->event, event)) {
            return RE_NOOP_DUPLICATE;
        }
        return durable_event_conflict_fault(store, event, &old_event->event,
                                            persist);
    }
    current = find_state(store, &event->allocation);
    has_state = current != NULL;
    if (memcmp(event->previous_chain_head, store->chain_head,
               RE_CHAIN_BYTES) != 0) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_CORRUPT;
    }
    if (has_state &&
        ((!field_equal(current->node, sizeof(current->node), event->node,
                       sizeof(event->node))) ||
         (event->kind != RE_EVENT_NODE_RESTART &&
          (!field_equal(current->boot_id, sizeof(current->boot_id),
                        event->boot_id, sizeof(event->boot_id)) ||
           !field_equal(current->authorized_peer,
                        sizeof(current->authorized_peer),
                        event->authorized_peer,
                        sizeof(event->authorized_peer)))))) {
        return durable_identity_fault(store, event, current, persist);
    }
    result = validate_global_sequence(store, event);
    if (result != RE_OK) {
        return durable_fault(store, event, 4U, persist);
    }
    memset(&staged, 0, sizeof(staged));
    if (has_state) {
        staged = *current;
    }
    result = transition_state(store, event, &staged, has_state);
    if (result != RE_OK && result != RE_UNKNOWN_RESULT) {
        return durable_fault(store, event, 5U, persist);
    }
    if (event->kind == RE_EVENT_ALLOCATION_STARTED &&
        node_is_blocked(store, event->node)) {
        return durable_fault(store, event, 6U, persist);
    }
    if ((!has_state && store->state_count >= store->state_capacity &&
         !ensure_state_capacity(store)) ||
        store->event_index_count >= RE_MAX_IDEMPOTENCY) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_BUSY;
    }
    auth_copy = malloc(event->authenticated_bytes_len);
    if (auth_copy == NULL) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    memcpy(auth_copy, event->authenticated_bytes,
           event->authenticated_bytes_len);
    if (persist && !append_event_record(store, event)) {
        free(auth_copy);
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    if (store->verify_commit == NULL ||
        !store->verify_commit(&event->allocation, event->event_id,
                              event->node_sequence,
                              store->durable_record_count + 1U,
                              event->previous_chain_head,
                              event->record_chain_head,
                              false,
                              store->verifier_context)) {
        if (persist) {
            (void)durable_fault(store, event, 7U, true);
        }
        free(auth_copy);
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_UNAUTHORIZED;
    }
    if (persist && !append_commit_record(store, event)) {
        free(auth_copy);
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_IO;
    }
    if (!has_state) {
        current = &store->states[store->state_count++];
    }
    staged.last_sequence = event->node_sequence;
    commit_last_event(&staged, event, auth_copy);
    *current = staged;
    memcpy(store->chain_head, event->record_chain_head, RE_CHAIN_BYTES);
    ++store->durable_record_count;
    if (!record_sequence(store, event, result == RE_UNKNOWN_RESULT) ||
        !remember_event(store, event)) {
        store->recovery_failed = true;
        force_all_unknown(store);
        return RE_BUSY;
    }
    return result;
}

static re_result replay_failure(re_store *store, re_result result)
{
    store->recovery_failed = true;
    store->recovered = false;
    force_all_unknown(store);
    return result;
}

static re_result replay_locked(re_store *store)
{
    re_record_header header;
    re_event_index_entry *pending;
    size_t pending_count = 0U;
    bool saw_initialization = false;

    pending = calloc(RE_MAX_IDEMPOTENCY, sizeof(*pending));
    if (pending == NULL) {
        return replay_failure(store, RE_IO);
    }
    if (lseek(store->fd, 0, SEEK_SET) < 0) {
        free(pending);
        return replay_failure(store, RE_IO);
    }
    for (;;) {
        ssize_t read_count = read(store->fd, &header, sizeof(header));
        if (read_count == 0) {
            break;
        }
        if (read_count < 0 && errno == EINTR) {
            continue;
        }
        if (read_count != (ssize_t)sizeof(header) ||
            header.magic != RE_JOURNAL_MAGIC ||
            header.version != RE_JOURNAL_VERSION ||
            header.body_len > RE_MAX_RECORD) {
            free(pending);
            return replay_failure(store, RE_CORRUPT);
        }
        if (header.kind == RE_JOURNAL_INIT_KIND) {
            uint32_t anchor_len;
            uint8_t anchor[RE_MAX_ANCHOR_BYTES];

            if (saw_initialization ||
                header.body_len < sizeof(re_init_record) - RE_MAX_ANCHOR_BYTES ||
                header.body_len > sizeof(re_init_record) - RE_MAX_ANCHOR_BYTES +
                                       RE_MAX_ANCHOR_BYTES ||
                !read_full(store->fd, store->chain_head,
                           sizeof(store->chain_head)) ||
                !nonzero_chain_head(store->chain_head) ||
                !read_full(store->fd, &anchor_len, sizeof(anchor_len)) ||
                anchor_len != header.body_len - sizeof(store->chain_head) -
                               sizeof(anchor_len) ||
                anchor_len == 0U ||
                !read_full(store->fd, anchor, anchor_len) ||
                memcmp(store->chain_head, store->initialization_chain_head,
                       RE_CHAIN_BYTES) != 0 ||
                store->initialization_anchor_len != anchor_len ||
                memcmp(store->initialization_anchor, anchor, anchor_len) != 0 ||
                store->verify_anchor == NULL ||
                !store->verify_anchor(anchor, anchor_len,
                                      store->verifier_context)) {
                free(pending);
                return replay_failure(store, RE_UNINITIALIZED);
            }
            saw_initialization = true;
            store->initialized = true;
            continue;
        }
        if (!saw_initialization) {
            free(pending);
            return replay_failure(store, RE_UNINITIALIZED);
        }
        if (header.kind == RE_JOURNAL_FAULT_KIND) {
            re_fault_record fault;
            if (header.body_len != sizeof(fault) ||
                !read_full(store->fd, &fault, sizeof(fault)) ||
                !nonzero_chain_head(fault.previous_chain_head) ||
                !nonzero_chain_head(fault.record_chain_head) ||
                fault.durable_record_count != store->durable_record_count + 1U ||
                memcmp(fault.previous_chain_head, store->chain_head,
                       RE_CHAIN_BYTES) != 0 ||
                !terminated_field(fault.node, sizeof(fault.node)) ||
                !terminated_field(fault.boot_id, sizeof(fault.boot_id)) ||
                store->verify_commit == NULL ||
                !store->verify_commit(&fault.key, fault.event_id,
                                      fault.node_sequence,
                                      fault.durable_record_count,
                                      fault.previous_chain_head,
                                      fault.record_chain_head,
                                      true,
                                      store->verifier_context) ||
                !mark_fault_state(store, &fault)) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            memcpy(store->chain_head, fault.record_chain_head,
                   RE_CHAIN_BYTES);
            ++store->durable_record_count;
            continue;
        }
        if (header.kind == RE_JOURNAL_COMMIT_KIND) {
            re_commit_record commit;
            size_t i;
            bool found = false;

            if (header.body_len != sizeof(commit) ||
                !read_full(store->fd, &commit, sizeof(commit)) ||
                !bounded_string(commit.node, sizeof(commit.node)) ||
                !bounded_string(commit.boot_id, sizeof(commit.boot_id))) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            for (i = 0; i < RE_MAX_IDEMPOTENCY; ++i) {
                re_event_index_entry *candidate = &pending[i];
                if (!candidate->used ||
                    memcmp(candidate->event.event_id, commit.event_id,
                           RE_EVENT_ID_BYTES) != 0) {
                    continue;
                }
                if (!key_equal(&candidate->event.allocation, &commit.key) ||
                    !field_equal(candidate->event.node,
                                 sizeof(candidate->event.node), commit.node,
                                 sizeof(commit.node)) ||
                    !field_equal(candidate->event.boot_id,
                                 sizeof(candidate->event.boot_id),
                                 commit.boot_id, sizeof(commit.boot_id)) ||
                    candidate->event.node_sequence != commit.node_sequence) {
                    free(pending);
                    return replay_failure(store, RE_CORRUPT);
                }
                if (commit.durable_record_count !=
                        store->durable_record_count + 1U ||
                    memcmp(commit.previous_chain_head, store->chain_head,
                           RE_CHAIN_BYTES) != 0 ||
                    memcmp(commit.record_chain_head,
                           candidate->event.record_chain_head,
                           RE_CHAIN_BYTES) != 0 ||
                    memcmp(commit.previous_chain_head,
                           candidate->event.previous_chain_head,
                           RE_CHAIN_BYTES) != 0) {
                    free(pending);
                    return replay_failure(store, RE_CORRUPT);
                }
                {
                    re_result result = apply_event_locked(
                        store, &candidate->event, false);
                    if (result != RE_OK && result != RE_NOOP_DUPLICATE &&
                        result != RE_UNKNOWN_RESULT) {
                        free(pending);
                        return replay_failure(store, result);
                    }
                }
                candidate->used = false;
                --pending_count;
                found = true;
                break;
            }
            if (!found) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            continue;
        }
        if (header.kind == RE_JOURNAL_PREPARE_KIND) {
            re_disk_event disk_event;
            re_event_index_entry *candidate = NULL;
            size_t i;
            size_t auth_len;

            if (header.body_len < sizeof(disk_event)) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            auth_len = header.body_len - sizeof(disk_event);
            if (auth_len == 0U || auth_len > RE_MAX_AUTH_BYTES ||
                !read_full(store->fd, &disk_event, sizeof(disk_event))) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            for (i = 0; i < RE_MAX_IDEMPOTENCY; ++i) {
                if (!pending[i].used) {
                    candidate = &pending[i];
                    break;
                }
            }
            if (candidate == NULL ||
                !read_full(store->fd, candidate->authenticated_bytes, auth_len)) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            candidate->event = disk_event.event;
            candidate->event.authenticated_bytes =
                candidate->authenticated_bytes;
            candidate->event.authenticated_bytes_len = auth_len;
            candidate->authenticated_bytes_len = (uint32_t)auth_len;
            candidate->used = true;
            ++pending_count;
            if (pending_count > RE_MAX_IDEMPOTENCY ||
                !event_shape_valid(&candidate->event)) {
                free(pending);
                return replay_failure(store, RE_CORRUPT);
            }
            continue;
        }
        free(pending);
        return replay_failure(store, RE_CORRUPT);
    }
    if (!saw_initialization || pending_count != 0U) {
        free(pending);
        return replay_failure(store, pending_count != 0U ?
                                      RE_CORRUPT : RE_UNINITIALIZED);
    }
    if (lseek(store->fd, 0, SEEK_END) < 0) {
        free(pending);
        return replay_failure(store, RE_IO);
    }
    if (store->verify_recovery == NULL ||
        !store->verify_recovery(store->initialization_anchor,
                                store->initialization_anchor_len,
                                store->durable_record_count,
                                store->chain_head,
                                store->verifier_context)) {
        free(pending);
        return replay_failure(store, RE_CORRUPT);
    }
    free(pending);
    store->recovered = true;
    return RE_OK;
}

static void destroy_store(re_store *store)
{
    size_t i;

    if (store == NULL) {
        return;
    }
    if (store->fd >= 0) {
        (void)flock(store->fd, LOCK_UN);
        (void)close(store->fd);
    }
    for (i = 0; i < store->state_count; ++i) {
        free(store->states[i].last_authenticated_bytes);
    }
    free(store->states);
    free(store->node_boot_sequences);
    free(store->event_index);
    if (store->mutex_ready) {
        (void)pthread_mutex_destroy(&store->mutex);
    }
    free(store);
}

re_result re_store_initialize(const re_store_options *options,
                              const char *journal_path,
                              const uint8_t *anchor, size_t anchor_len)
{
    int journal_fd;

    if (options == NULL || journal_path == NULL || anchor == NULL ||
        anchor_len == 0U || anchor_len > RE_MAX_ANCHOR_BYTES ||
        options->verify_anchor == NULL || options->verify_event == NULL ||
        options->verify_commit == NULL || options->verify_recovery == NULL ||
        !nonzero_chain_head(options->initialization_chain_head) ||
        !options->prototype_enable ||
        RELEASE_EVIDENCE_ENABLED == 0) {
        return RE_DISABLED;
    }
    if (!options->verify_anchor(anchor, anchor_len,
                                options->verifier_context)) {
        return RE_UNAUTHORIZED;
    }
    journal_fd = open_locked_journal(journal_path, true);
    if (journal_fd < 0) {
        return errno == EEXIST ? RE_BUSY : RE_IO;
    }
    if (!append_initialization_record(journal_fd, anchor, anchor_len,
                                      options->initialization_chain_head)) {
        (void)flock(journal_fd, LOCK_UN);
        (void)close(journal_fd);
        return RE_IO;
    }
    (void)flock(journal_fd, LOCK_UN);
    (void)close(journal_fd);
    return RE_OK;
}

re_result re_store_open(re_store **out, const re_store_options *options,
                        const char *journal_path)
{
    re_store *store;
    int journal_fd;
    re_result replay_result;

    if (out == NULL || options == NULL || journal_path == NULL ||
        options->verify_event == NULL || options->verify_anchor == NULL ||
        options->verify_commit == NULL || options->verify_recovery == NULL ||
        options->initialization_anchor == NULL ||
        options->initialization_anchor_len == 0U ||
        options->initialization_anchor_len > RE_MAX_ANCHOR_BYTES ||
        !nonzero_chain_head(options->initialization_chain_head) ||
        !options->prototype_enable ||
        RELEASE_EVIDENCE_ENABLED == 0) {
        return RE_DISABLED;
    }
    journal_fd = open_locked_journal(journal_path, false);
    if (journal_fd < 0) {
        if (errno == EWOULDBLOCK || errno == EAGAIN) {
            return RE_BUSY;
        }
        return errno == ENOENT ? RE_NOT_FOUND : RE_IO;
    }
    store = calloc(1U, sizeof(*store));
    if (store == NULL) {
        (void)close(journal_fd);
        return RE_IO;
    }
    store->fd = journal_fd;
    store->verify_event = options->verify_event;
    store->verify_admin = options->verify_admin;
    store->verify_anchor = options->verify_anchor;
    store->verify_commit = options->verify_commit;
    store->verify_recovery = options->verify_recovery;
    store->verifier_context = options->verifier_context;
    store->prototype_enable = options->prototype_enable;
    store->initialization_anchor_len = options->initialization_anchor_len;
    memcpy(store->initialization_anchor, options->initialization_anchor,
           options->initialization_anchor_len);
    memcpy(store->chain_head, options->initialization_chain_head,
           RE_CHAIN_BYTES);
    memcpy(store->initialization_chain_head,
           options->initialization_chain_head, RE_CHAIN_BYTES);
    store->node_boot_sequences =
        calloc(RE_MAX_NODE_BOOT_TRACKERS, sizeof(*store->node_boot_sequences));
    store->event_index =
        calloc(RE_MAX_IDEMPOTENCY, sizeof(*store->event_index));
    if (store->node_boot_sequences == NULL || store->event_index == NULL ||
        pthread_mutex_init(&store->mutex, NULL) != 0) {
        destroy_store(store);
        return RE_IO;
    }
    store->mutex_ready = true;
    (void)pthread_mutex_lock(&store->mutex);
    replay_result = replay_locked(store);
    (void)pthread_mutex_unlock(&store->mutex);
    if (replay_result != RE_OK) {
        destroy_store(store);
        return replay_result;
    }
    *out = store;
    return RE_OK;
}

void re_store_close(re_store *store)
{
    if (store == NULL) {
        return;
    }
    (void)pthread_mutex_lock(&store->mutex);
    store->recovered = false;
    (void)pthread_mutex_unlock(&store->mutex);
    destroy_store(store);
}

re_result re_store_replay(re_store *store)
{
    re_result result;

    if (store == NULL || !store->prototype_enable) {
        return RE_DISABLED;
    }
    (void)pthread_mutex_lock(&store->mutex);
    if (store->recovery_failed) {
        result = RE_CORRUPT;
    } else if (store->recovered) {
        result = RE_OK;
    } else {
        result = replay_locked(store);
    }
    (void)pthread_mutex_unlock(&store->mutex);
    return result;
}

re_result re_store_apply(re_store *store, const re_event *event)
{
    re_result result;

    if (store == NULL || event == NULL || !store->prototype_enable ||
        RELEASE_EVIDENCE_ENABLED == 0) {
        return RE_DISABLED;
    }
    (void)pthread_mutex_lock(&store->mutex);
    if (!store->recovered) {
        result = RE_UNKNOWN_RESULT;
    } else if (store->recovery_failed) {
        result = RE_CORRUPT;
    } else {
        result = apply_event_locked(store, event, true);
    }
    (void)pthread_mutex_unlock(&store->mutex);
    return result;
}

re_result re_store_observe(const re_store *store,
                           const re_allocation_key *key,
                           re_observation *out)
{
    const re_generation_state *state;
    re_store *mutable_store;
    re_result result;

    if (store == NULL || key == NULL || out == NULL) {
        return RE_INVALID;
    }
    mutable_store = (re_store *)store;
    (void)pthread_mutex_lock(&mutable_store->mutex);
    if (!store->recovered || store->recovery_failed) {
        memset(out, 0, sizeof(*out));
        out->state = RE_UNKNOWN;
        out->usage_must_remain_held = true;
        result = RE_UNKNOWN_RESULT;
    } else {
        state = find_state_const(store, key);
        if (state == NULL) {
            result = RE_NOT_FOUND;
        } else {
            memset(out, 0, sizeof(*out));
            if (state->state == RE_RELEASED &&
                node_is_blocked(store, state->node)) {
                out->state = RE_UNKNOWN;
                out->release_allowed = false;
                out->usage_must_remain_held = true;
                result = RE_UNKNOWN_RESULT;
            } else {
                out->state = state->state;
                out->release_allowed = state->state == RE_RELEASED;
                out->usage_must_remain_held = state->usage_must_remain_held;
                result = RE_OK;
            }
            out->last_node_sequence = state->last_sequence;
            (void)snprintf(out->boot_id, sizeof(out->boot_id), "%s",
                           state->boot_id);
        }
    }
    (void)pthread_mutex_unlock(&mutable_store->mutex);
    return result;
}

re_result re_store_admission_gate(const re_store *store, const char *node)
{
    re_store *mutable_store;
    re_result result;

    if (store == NULL || !store->prototype_enable ||
        RELEASE_EVIDENCE_ENABLED == 0) {
        return RE_DISABLED;
    }
    if (!bounded_string(node, RE_NODE_MAX)) {
        return RE_INVALID;
    }
    mutable_store = (re_store *)store;
    (void)pthread_mutex_lock(&mutable_store->mutex);
    if (!store->recovered || store->recovery_failed ||
        node_is_blocked(store, node)) {
        result = RE_UNKNOWN_RESULT;
    } else {
        result = RE_OK;
    }
    (void)pthread_mutex_unlock(&mutable_store->mutex);
    return result;
}
