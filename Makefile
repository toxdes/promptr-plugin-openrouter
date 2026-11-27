CC       ?= gcc
CFLAGS   ?= -std=c11 -Wall -Wextra -Werror -pedantic -O2
ifeq ($(BUILD),debug)
CFLAGS = -std=c11 -Wall -Wextra -pedantic -Og -ggdb3
endif

INCS     := $(shell pkg-config --cflags gio-2.0 json-glib-1.0 libcurl)
LIBS     := $(shell pkg-config --libs gio-2.0 json-glib-1.0 libcurl)

SDK_SOURCES := lib/promptr-protocol/jsonrpc.c lib/promptr-protocol/provider-server.c
SDK_HEADERS := lib/promptr-protocol/jsonrpc.h lib/promptr-protocol/provider-server.h
PLUGIN_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)/promptr/plugins/openrouter

promptr-openrouter: main.c $(SDK_SOURCES) $(SDK_HEADERS)
	$(CC) $(CFLAGS) $(INCS) -o $@ main.c $(SDK_SOURCES) $(LIBS)

debug:
	$(MAKE) BUILD=debug

install: promptr-openrouter
	install -Dm755 promptr-openrouter "$(PLUGIN_DIR)/promptr-openrouter"
	install -Dm644 plugin.json "$(PLUGIN_DIR)/plugin.json"

test: promptr-openrouter
	python3 tests/test_adapter.py

clean:
	rm -f promptr-openrouter

.PHONY: clean debug install test
