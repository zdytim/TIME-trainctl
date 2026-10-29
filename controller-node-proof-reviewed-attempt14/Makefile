CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror -pedantic -pthread
CPPFLAGS ?= -Iinclude -DRELEASE_EVIDENCE_ENABLED=0
BUILD_DIR ?= .build

.PHONY: compile-check clean

compile-check:
	$(CC) $(CPPFLAGS) $(CFLAGS) -fsyntax-only src/release_evidence.c
	mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c src/release_evidence.c -o $(BUILD_DIR)/release_evidence.o
	rm -f $(BUILD_DIR)/release_evidence.o

clean:
	rm -rf $(BUILD_DIR)
