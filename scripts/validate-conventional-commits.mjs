import { execFileSync } from "node:child_process";

const conventionalCommit = /^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\([^)]+\))?!?: \S.+$/;
const title = process.env.PR_TITLE?.trim();
const baseSha = process.env.BASE_SHA?.trim();
const headSha = process.env.HEAD_SHA?.trim() || "HEAD";

function validate(label, value) {
  if (!value || !conventionalCommit.test(value)) {
    console.error(`Invalid Conventional Commit ${label}: ${value || "<empty>"}`);
    return false;
  }
  return true;
}

let valid = true;
if (title) valid = validate("PR title", title) && valid;

const range = baseSha ? `${baseSha}..${headSha}` : headSha;
const subjects = execFileSync("git", ["log", "--no-merges", "--format=%s", range], {
  encoding: "utf8",
}).trim();

for (const subject of subjects ? subjects.split("\n") : []) {
  valid = validate("commit", subject) && valid;
}

if (!valid) {
  console.error("Use: type(scope): description");
  process.exit(1);
}
