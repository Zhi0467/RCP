import { createRoot } from "react-dom/client";
import { OperatorActionPanel } from "../../src/views/OperatorActionPanel";
import type { ServerStep } from "../../src/types";
import "../../src/styles.css";

// The exact stop `deploy_key_operator_step` builds, so the panel is measured
// against real text lengths rather than convenient short ones.
const step: ServerStep = {
  number: 2,
  title: "Add a deploy key on GitHub",
  purpose: "Give one central checkout its repository-scoped GitHub write identity.",
  performed_by: "human",
  target: {
    kind: "external_service",
    service: "github.com",
    resource: "zhi0467/rcp",
    destination_url: "https://github.com/zhi0467/rcp/settings/keys",
    required_authority_role: "repository administrator",
  },
  phase: "github_grant",
  state: "operator_action_needed",
  expected_success:
    "The request-scoped Git push is read back exactly and its temporary ref is removed.",
  message:
    "GitHub has not yet proven read and write access for this repository-scoped deploy key. Complete the displayed grant and host-trust steps, then resume.",
  actions: [
    {
      kind: "external",
      instruction:
        "Open https://github.com/zhi0467/rcp/settings/keys; add the displayed public key with title 'rcp:7eb4ea9d-cccf-42fd-abfe-09f71f4b8cd2:2ad064a6-f015-4703-a223-1d64cde75cc8:paper', and enable Allow write access.",
    },
    {
      kind: "command",
      argv: [
        "sudo",
        "-n",
        "-u",
        "rcp",
        "-H",
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        "/var/folders/fy/n_7qxz0s3h32g3p8_lnj6c6c0000gn/T/tmp24wdmybh/home/rcp/rcp-server/credentials/projects/2ad064a6-f015-4703-a223-1d64cde75cc8/paper/id_ed25519",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=ask",
        "-o",
        "GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts",
        "-o",
        "UserKnownHostsFile=/var/folders/fy/n_7qxz0s3h32g3p8_lnj6c6c0000gn/T/tmp24wdmybh/home/rcp/.ssh/known_hosts",
        "-T",
        "git@github.com",
      ],
      execution: {
        kind: "server_shell",
        shell_account: null,
      },
    },
    {
      kind: "external",
      instruction:
        "Before accepting GitHub's host key, compare its fingerprint with https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints. A successful no-shell authentication may exit with status 1.",
    },
  ],
  fields: [
    {
      name: "deploy_key_label",
      value: "rcp:7eb4ea9d-cccf-42fd-abfe-09f71f4b8cd2:2ad064a6-f015-4703-a223-1d64cde75cc8:paper",
    },
    {
      name: "deploy_public_key",
      value:
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA rcp:7eb4ea9d-cccf-42fd-abfe-09f71f4b8cd2:2ad064a6-f015-4703-a223-1d64cde75cc8:paper",
    },
    {
      name: "public_key_fingerprint",
      value: "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    },
  ],
  resume_argv: [
    "sudo",
    "-n",
    "-u",
    "rcp",
    "-H",
    "/usr/local/bin/rcp",
    "server",
    "project",
    "provision",
    "a29ddba0-a0a7-46be-ab7a-7a6d77644ea5",
  ],
  resume_execution: {
    kind: "server_shell",
    shell_account: null,
  },
} as ServerStep;

// A direct route signs in as the service account itself, so the same stop must
// render differently under each saved route.
const mode = new URLSearchParams(window.location.search).get("mode");
const route =
  mode === "direct_rcp"
    ? ({ ssh_target: "rcp@server.example", mode: "direct_rcp" } as const)
    : ({ ssh_target: "operator@server.example", mode: "sudo_rcp" } as const);

createRoot(document.getElementById("root")!).render(
  <div style={{ padding: 24, maxWidth: 760 }}>
    <OperatorActionPanel
      step={step}
      route={route}
      onRefresh={() => {
        document.body.dataset.refreshed = "yes";
      }}
    />
  </div>,
);
