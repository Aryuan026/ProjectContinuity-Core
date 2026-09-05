#!/usr/bin/env node

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2048;

function fail() {
  process.stderr.write("TeamAI literal recall request is invalid\n");
  process.exitCode = 2;
}

async function readRequest() {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input, "utf8") > MAX_INPUT_BYTES) {
      throw new Error("input_too_large");
    }
  }
  const value = JSON.parse(input);
  if (
    value === null ||
    Array.isArray(value) ||
    typeof value !== "object" ||
    Object.keys(value).length !== 1 ||
    !Object.hasOwn(value, "query") ||
    typeof value.query !== "string" ||
    value.query.length === 0 ||
    value.query.length > 500 ||
    value.query !== value.query.trim() ||
    [...value.query].some((character) => {
      const code = character.codePointAt(0);
      return code < 32 || code === 127;
    })
  ) {
    throw new Error("query_invalid");
  }
  return value.query;
}

async function main() {
  if (process.argv.length !== 3) {
    throw new Error("entrypoint_invalid");
  }
  const entrypoint = process.argv[2];
  const query = await readRequest();
  const require = createRequire(pathToFileURL(entrypoint));
  const { Command } = require("commander");
  const originalAction = Command.prototype.action;
  const originalParse = Command.prototype.parse;
  let recallAction;
  let recallCommand;
  let invocation;

  Command.prototype.action = function registerAction(action) {
    if (this.name() === "recall" && this.parent) {
      recallAction = action;
      recallCommand = this;
    }
    return originalAction.call(this, action);
  };
  Command.prototype.parse = function invokeLiteralRecall() {
    if (!recallAction || !recallCommand || invocation) {
      throw new Error("recall_action_unavailable");
    }
    this.setOptionValue("dryRun", true);
    invocation = Promise.resolve(
      recallAction.call(
        recallCommand,
        [query],
        { check: false, depth: "context" },
        recallCommand,
      ),
    );
    return this;
  };

  try {
    await import(pathToFileURL(entrypoint).href);
    if (!invocation) {
      throw new Error("recall_action_unavailable");
    }
    await invocation;
  } finally {
    Command.prototype.action = originalAction;
    Command.prototype.parse = originalParse;
  }
}

try {
  await main();
} catch {
  fail();
}
