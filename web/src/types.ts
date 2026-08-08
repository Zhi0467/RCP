export type Standing = "asserted" | "accepted" | "contested";
export type TrustView = "working" | "accepted" | "review";
export type AppView =
  "overview" | "attention" | "scientific" | "dag" | "execution" | "paper" | "settings" | "chats";
export type AgentSurface = "seed" | "refresh" | "node_chat" | "project_chat" | "paper_coach";
export type AgentTaskKind = AgentSurface;
export type AgentTaskStatus =
  "queued" | "running" | "pausing" | "paused" | "succeeded" | "failed" | "interrupted";
export type ConversationMode = "discuss" | "work";
export type TaskTrigger = "human" | "experiment_run" | "watcher";
export type GraphPatchKind = "work" | "experiment_loop";
export type AgentCapability = "discuss" | "work_auto" | "scratch_patch" | "paper_readonly";

export interface Health {
  status: string;
  agent_mode: "provider" | "acceptance";
  version: string;
  instance_id: string;
  data_dir_id: string;
  owner_kind: string;
  active_agent_tasks: number;
  pid: number;
  projects?: number;
  project?: string | null;
}

export interface SourceRef {
  machine: string;
  truth_repository: string;
  source: string;
  session_id: string;
  record_uuid: string;
  timestamp: string;
  excerpt: string;
}

export interface GraphNode {
  id: string;
  type: "research_question" | "hypothesis" | "decision" | "experiment" | "evidence" | "blocker";
  title: string;
  extension_type?: string | null;
  extension_fields: Record<string, string | number | boolean | string[]>;
  standing: Standing;
  created_rev: number;
  updated_rev: number;
  source_refs: SourceRef[];
  status?: string;
  question?: string;
  statement?: string;
  scope?: string;
  objective?: string;
  observation?: string;
  description?: string;
  current_summary?: string;
  next_action?: string | null;
  blocker_type?: string;
  validity?: string;
  origin?: "internal_run" | "external_publication" | "external_instance" | "analytic" | "unknown";
  attempts?: ExperimentAttempt[];
  invocation_ceiling?: number;
  completion_criteria?: string[];
  draft_touched?: boolean;
  [key: string]: unknown;
}

export type ExtensionFieldValue = string | number | boolean | string[];

export interface ExperimentAttempt {
  id: string;
  sequence: number;
  purpose: string;
  attempt_kind: "external_run" | "proposal_only";
  decision_bundle: ExperimentDecisionPin[];
  debug?: ExperimentAttemptDebug | null;
  status: string;
  outcome?: string | null;
  failure_reason?: string | null;
  job_refs: string[];
}

export interface ExperimentDecisionPin {
  decision_id: string;
  decision_revision: number;
  selected_option: string;
}

export interface ExperimentAttemptDebug {
  mechanical_fault: string;
  change: string;
  predicted_effect: string;
}

export interface DecisionDrift {
  decision_id: string;
  pinned_option: string;
  pinned_revision: number;
  current_option: string | null;
  current_status: string | null;
}

export interface ExperimentSessionBinding {
  provider: string | null;
  model: string | null;
  reasoning: string | null;
  run_on: string | null;
  execution_host: string | null;
  run_truth_scope: string[] | null;
  native_session_bound: boolean;
  diagnostic: string | null;
}

export interface ExperimentOperationalState {
  task_active: boolean;
  detached_work_active: boolean;
  watcher_degraded: boolean;
  watcher_completion_pending: boolean;
  episode_exited: boolean;
  stop_requested: boolean;
  stop_settled: boolean;
  chat_id: string | null;
  current_operation_id: string | null;
  current_status: string | null;
  current_phase: string | null;
  current_status_message: string | null;
  current_last_activity_at: string | null;
  current_invocation: number | null;
  session: ExperimentSessionBinding;
}

export interface ExperimentControlState {
  ready: boolean;
  reasons: string[];
  invocations_used: number;
  invocation_ceiling: number;
  invocations_remaining: number;
  episode_id: string | null;
  paused: boolean;
  active: boolean;
  governing_decisions: ExperimentDecisionPin[];
  decision_drift: DecisionDrift[];
  operational: ExperimentOperationalState;
}

export interface WatcherContinuation {
  provider: string;
  model: string | null;
  reasoning: string | null;
  run_on: string;
  run_truth_scope: string[] | null;
  patch_kind: "work" | "experiment_loop";
  control_node_id: string | null;
  control_revision: number | null;
  control_episode_id: string | null;
  control_invocation: number | null;
  control_invocation_ceiling: number | null;
  control_decision_bundle: Record<string, unknown>[];
  control_completion_criteria: string[];
  workflow_ids: string[];
  skill_ids: string[];
  invoked_workflow_ids: string[];
  invoked_skill_ids: string[];
  resolved_skill_packages: SkillReference[];
}

export interface WatcherRecord {
  watcher_id: string;
  project_id: string;
  origin_operation_id: string;
  origin_task_kind: "node_chat" | "project_chat";
  chat_id: string;
  node_id: string | null;
  experiment_episode_id: string | null;
  execution_host: string;
  check_command: string;
  log_path: string;
  cwd: string;
  continuation: WatcherContinuation;
  status: "active" | "degraded" | "completed" | "stopped";
  created_at: string;
  last_checked_at: string | null;
  last_exit_code: number | null;
  last_error: string | null;
  completed_at: string | null;
  next_check_at: string | null;
  consecutive_error_count: number;
  group_id: string | null;
  group_label: string | null;
  notified: boolean;
  notification_operation_id: string | null;
  stopped_by: "human" | "loop" | "agent" | null;
  stop_reason: string | null;
  stopped_at: string | null;
  stop_operation_id: string | null;
}

export interface Edge {
  id: string;
  source: string;
  target: string;
  relation: string;
  layer: "epistemic" | "action" | "seam" | "meta";
  explanation: string;
}

export type BaseNodeType = GraphNode["type"];
export type OntologyLayer = "epistemic" | "action";
export type OntologyFieldKind = "text" | "number" | "boolean" | "text_list";

export interface OntologyTypeDefinition {
  name: string;
  definition: string;
  base_type: BaseNodeType;
  layer: OntologyLayer;
  deprecated: boolean;
}

export interface OntologyFieldDefinition {
  owner_type: string;
  name: string;
  definition: string;
  kind: OntologyFieldKind;
  required: boolean;
  agent_writable: boolean;
  deprecated: boolean;
}

export interface OntologyRelationDefinition {
  name: string;
  definition: string;
  source_types: string[];
  target_types: string[];
  layer: OntologyLayer;
  deprecated: boolean;
}

export interface OntologyState {
  types: OntologyTypeDefinition[];
  fields: OntologyFieldDefinition[];
  relations: OntologyRelationDefinition[];
}

export interface BeliefTransition {
  hypothesis_id: string;
  from_status: string;
  to_status: string;
  revision: number;
  cause:
    | { kind: "evidence_edge" | "decision" | "proposal_resolution"; ref_id: string }
    | { kind: "human_edit" };
}

export interface ReplayFailure {
  revision: number;
  created_at: string;
  code: string;
  message: string;
}

export interface Proposal {
  id: string;
  title: string;
  card: {
    situation_cold: string;
    why_human_now: string;
    consequences: string;
    decision_needed: string;
  };
  ops: Record<string, unknown>[];
  related_node_ids: string[];
  base_rev: number;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  created_by?: "agent" | "human";
  created_by_operation_id?: string | null;
  resolved_by?: "agent" | "human" | null;
  resolved_by_operation_id?: string | null;
  resolution_reason?: string | null;
}

export interface Ambiguity {
  id: string;
  question: string;
  why_it_matters: string;
  related_node_ids: string[];
  status: string;
}

export interface GraphState {
  revision: number;
  nodes: Record<string, GraphNode>;
  edges: Record<string, Edge>;
  proposals: Record<string, Proposal>;
  ambiguities: Record<string, Ambiguity>;
  glossary: Record<string, GlossaryTerm>;
  ontology: OntologyState;
  validation_messages: ValidationMessage[];
  belief_transitions: BeliefTransition[];
  replay_status: "complete" | "degraded";
  replay_failure?: ReplayFailure | null;
}

export interface GlossaryTerm {
  term: string;
  plain_definition: string;
  where_defined?: string | null;
}

export interface ValidationMessage {
  level: "flag" | "reject";
  code: string;
  message: string;
  patch_revision?: number | null;
  related_node_ids: string[];
  related_edge_ids: string[];
}

export interface Repository {
  alias: string;
  machine: string;
  path: string;
}

export interface Machine {
  alias: string;
  host: string;
  provider_paths: Record<ProviderId, string>;
}

export interface AgentPermissions {
  read_graph: boolean;
  read_research_md: boolean;
  read_introduction: boolean;
  read_repositories: "none" | "run_scope" | "project_scope";
  read_conversations: "none" | "run_scope";
  write_graph_patch: boolean;
  write_project_files: boolean;
  write_paper: boolean;
}

/**
 * A provider id. The backend registry in `src/rcp/providers.py` is the only
 * place providers are enumerated; the frontend never hardcodes one, and reads
 * ids, labels, models, and reasoning efforts out of `provider_readiness`.
 */
export type ProviderId = string;

/** One model a provider accepts, with the reasoning efforts that model supports. */
export interface ModelChoice {
  id: string;
  label: string;
  reasoning: string[];
  default_reasoning: string;
}

export interface AgentProfile {
  provider: ProviderId;
  model: string;
  reasoning: string;
  run_on: string;
  permissions: AgentPermissions;
}

export interface AgentRunConfig {
  provider: ProviderId;
  model: string;
  reasoning: string;
  run_on: string;
}

export interface AgentTaskReceipt {
  receipt_id: number;
  operation_id: string;
  created_at: string;
  tier: "summary" | "diagnostic" | "trace";
  category: string;
  payload: Record<string, unknown>;
}

export interface AgentTaskContract {
  operation_id: string;
  role: string;
  created_at: string;
  sha256: string;
  content: string;
}

export interface AgentTaskRequest {
  provider?: ProviderId | null;
  model?: string | null;
  reasoning?: string | null;
  run_on?: string | null;
  run_truth_scope?: string[] | null;
  chat_scope?: "node" | "project";
  node_id?: string | null;
  message?: string | null;
  chat_id?: string | null;
  attachment_set_id?: string | null;
  attachment_client_id?: string | null;
  attachments?: ChatAttachmentDescriptor[];
  session_id?: string | null;
  mode?: ConversationMode;
  trigger?: TaskTrigger;
  patch_kind?: GraphPatchKind;
  control_node_id?: string | null;
  control_revision?: number | null;
  control_decision_bundle?: ExperimentDecisionPin[];
  control_completion_criteria?: string[];
  watcher_ids?: string[];
  workflow_ids?: string[] | null;
  skill_ids?: string[] | null;
  invoked_workflow_ids?: string[];
  invoked_skill_ids?: string[];
  invoked_provider_skill_names?: string[];
  resolved_provider_skills?: ProviderSkillReference[];
  resolved_skill_packages?: SkillReference[] | null;
  [key: string]: unknown;
}

export type SkillKind = "skill" | "workflow";

export interface SkillReference {
  id: string;
  kind: SkillKind;
  version: string;
}

export interface SkillCatalogEntry extends SkillReference {
  label: string;
  description: string;
  dependencies: SkillReference[];
}

export interface SkillPackageDetail extends SkillCatalogEntry {
  body: string;
}

export interface SkillDefaults {
  workflow_ids: string[];
  skill_ids: string[];
}

export interface ProviderSkill {
  name: string;
  label: string;
  description: string;
  scope?: string | null;
  path?: string | null;
  enabled: boolean;
}

export interface ProviderSkillInventory {
  provider: ProviderId;
  machine: string;
  host: string;
  configured_binary?: string | null;
  resolved_binary?: string | null;
  provider_version?: string | null;
  inventory_hash?: string | null;
  refreshed_at?: string | null;
  command: string[];
  protocol?: "jsonrpc" | "jsonl" | null;
  status: "refreshing" | "fresh" | "stale" | "unavailable";
  stale: boolean;
  diagnostic?: string | null;
  skills: ProviderSkill[];
}

export interface ProviderSkillReference {
  provider: ProviderId;
  machine: string;
  provider_version: string;
  inventory_hash: string;
  name: string;
  label: string;
  description: string;
  stale: boolean;
}

export interface GraphUpdateResult {
  status: "none" | "applied" | "rejected";
  applied_revision: number | null;
  change_summary: string[];
  proposal_ids: string[];
  validation_messages: string[];
  correction_rounds: number;
  repairable: boolean;
}

export interface RevisionSummary {
  from_revision: number;
  to_revision: number;
  kind: "seed" | "refresh" | "chat" | "work" | "experiment_loop" | "approval";
  author: "agent" | "human";
  created_at: string;
  sentences: string[];
}

export interface AgentTaskResult {
  messages?: string[];
  artifacts?: AgentArtifactDescriptor[];
  graph_update?: GraphUpdateResult;
  [key: string]: unknown;
}

export type AgentArtifactMediaType =
  "text/html" | "image/png" | "image/jpeg" | "image/gif" | "image/webp";

export interface AgentArtifactDescriptor {
  artifact_id: string;
  name: string;
  media_type: AgentArtifactMediaType;
}

export interface AgentTask {
  operation_id: string;
  project_id: string;
  kind: AgentTaskKind;
  status: AgentTaskStatus;
  request: AgentTaskRequest;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  status_message: string;
  error?: string | null;
  applied_revision?: number | null;
  result?: AgentTaskResult | null;
  attempt: number;
  parent_operation_id?: string | null;
  native_session_id?: string | null;
  stage_host?: string | null;
  stage_root?: string | null;
  estimate_seconds: number;
  estimate_samples: number;
  phase: string;
  last_activity_at?: string | null;
  elapsed_seconds: number;
  progress: number;
  can_pause: boolean;
  can_resume: boolean;
  can_retry: boolean;
  events?: AgentTaskEvent[];
  debug_receipts?: AgentTaskReceipt[];
  contracts?: AgentTaskContract[];
}

export interface AgentUsageRecord {
  usage_id: string;
  project_id: string;
  operation_id: string;
  task_kind: AgentTaskKind;
  provider: string;
  model?: string | null;
  provider_profile: string;
  provider_event_type: string;
  dedupe_key: string;
  counted: boolean;
  count_reason: "counted" | "duplicate" | "invalid";
  created_at: string;
  processed_input_tokens: number;
  generated_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  cache_write_input_tokens: number;
  reasoning_output_tokens: number;
  reported_input_tokens?: number | null;
  reported_output_tokens?: number | null;
  reported_total_tokens?: number | null;
  provider_fields: Record<string, unknown>;
}

export interface AgentUsageCell {
  task_kind: AgentTaskKind;
  provider: string;
  processed_input_tokens: number;
  generated_tokens: number;
  cached_input_tokens: number;
  counted_records: number;
}

export interface AgentUsageMetric {
  total_tokens: number;
  cached_tokens: number;
  cache_share: number;
  block_percent: number;
  block_tokens: number;
  cells: AgentUsageCell[];
}

export interface AgentUsageSnapshot {
  project_id: string;
  input_processed: AgentUsageMetric;
  generated: AgentUsageMetric;
  counted_records: number;
  excluded_records: number;
  records: AgentUsageRecord[];
}

export type StartAgentTask = (kind: AgentTaskKind, request: AgentTaskRequest) => Promise<AgentTask>;

export interface ChatSummary {
  chat_id: string;
  kind: "node_chat" | "project_chat";
  node_id: string | null;
  title: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string;
}

export interface ChatSummaryPage {
  items: ChatSummary[];
  total: number;
  offset: number;
  limit: number;
}

export interface ChatMessage {
  message_id: string;
  operation_id?: string | null;
  role: "user" | "assistant";
  text: string;
  timestamp: string;
  native_session_id: string | null;
  provider: string | null;
  model: string | null;
  reasoning: string | null;
  execution_machine: string | null;
  applied_revision: number | null;
  mode: ConversationMode | null;
  graph_update: GraphUpdateResult | null;
  trigger: TaskTrigger;
  attachments: ChatAttachmentDescriptor[];
}

export interface ChatAttachmentDescriptor {
  attachment_id: string;
  name: string;
  media_type: string;
  size: number;
  expires_at: string;
}

export interface ChatTranscript extends ChatSummary {
  messages: ChatMessage[];
}

export interface AgentTaskEvent {
  event_id: number;
  operation_id: string;
  created_at: string;
  level: "info" | "warning" | "error";
  message: string;
}

export interface ProviderReadiness {
  provider: ProviderId;
  label: string;
  installed: boolean;
  authenticated: boolean;
  binary_path: string | null;
  path_state: "resolved" | "missing" | "denied" | "unconfigured" | "unreachable";
  version?: string | null;
  reason?: string | null;
  models: ModelChoice[];
}

export interface PaperSnapshot {
  content: string;
  sync_state: "not_created" | "synced" | "unsynced" | "conflict";
  base_hash?: string | null;
  canonical_hash?: string | null;
  updated_at?: string | null;
  canonical_available: boolean;
}

export interface ProjectSnapshot {
  id: string;
  name: string;
  revision: number;
  state_repository: string;
  canonical_state: {
    remote: boolean;
    reachable: boolean;
    location: string;
    last_synced_at?: string | null;
    error?: string | null;
  };
  run_on: string;
  project_truth_scope: string[];
  default_run_truth_scope: string[];
  repositories: Repository[];
  machines: Machine[];
  primary_question?: GraphNode | null;
  last_refresh_at?: string | null;
  experiment_control: Record<string, ExperimentControlState>;
  counts: {
    pending_proposals: number;
    decisions_awaiting_choice: number;
    open_blockers: number;
    asserted: number;
    accepted: number;
    contested: number;
  };
  coverage: {
    repositories_seen: string[];
    repositories_never_seen: string[];
    sessions_read: string[];
    sessions_skipped: string[];
    earliest_timestamp?: string | null;
    note: string;
  };
  graph: GraphState;
  paper: PaperSnapshot;
  paper_coach: {
    default_provider: ProviderId;
    default_model: string;
    default_reasoning: string;
  };
  agent_profiles: Record<AgentSurface, AgentProfile>;
  skill_catalog: SkillCatalogEntry[];
  skill_defaults: SkillDefaults;
  provider_skill_inventories: Record<
    string,
    Partial<Record<ProviderId, ProviderSkillInventory | null>>
  >;
  provider_readiness: Record<string, Record<ProviderId, ProviderReadiness>>;
  providers: Record<ProviderId, ProviderReadiness>;
  cache_metrics: ProjectCacheMetrics;
  validation_messages: ValidationMessage[];
}

export interface CacheMetric {
  bytes: number;
  count: number;
  limits: {
    max_bytes: number;
    max_count: number;
    ttl_seconds: number;
  };
  oldest_accessed_at?: string | null;
  reclaimable_bytes: number;
  reclaimable_count: number;
}

export interface ProjectCacheMetrics {
  remote_sources: CacheMetric;
  session_slices: CacheMetric;
}

export interface ProjectCard {
  id: string;
  name: string;
  locator: string;
  state_location: string;
  remote: boolean;
  last_opened_at?: string | null;
  revision?: number | null;
  primary_question?: string | null;
  attention_count: number;
  last_refresh_at?: string | null;
  reachable?: boolean | null;
  error?: string | null;
}

export interface SetupRepository {
  alias: string;
  location: "local" | "ssh";
  path: string;
  host: string;
  default_read: boolean;
}

export interface SetupExecution {
  location: "local" | "ssh";
  host: string;
}

export interface SetupAgentProfile {
  provider: ProviderId;
  model: string;
  reasoning: string;
  location: "local" | "ssh";
  host: string;
}

export type SetupAgents = Record<AgentSurface, SetupAgentProfile>;

export interface ProjectSetupRequest {
  name: string;
  repositories: SetupRepository[];
  state_repository: string;
  execution: SetupExecution;
  agents: SetupAgents;
  confirmed: boolean;
}

export interface ProjectSettingsRequest {
  default_run_truth_scope: string[];
  agent_profiles: Record<AgentSurface, AgentRunConfig>;
  skill_defaults: SkillDefaults;
  machine_provider_paths?: Record<string, Record<ProviderId, string>>;
}

export interface ProviderPathResolution {
  machine: string;
  provider: ProviderId;
  binary_path: string | null;
  readiness: ProviderReadiness;
  project: ProjectSnapshot;
}

export interface SetupCheck {
  label: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}

export interface SetupPreview {
  checks: SetupCheck[];
  can_create: boolean;
  action: "create" | "connect";
  canonical_location: string;
  existing_project_name?: string | null;
  manifest_preview: string;
  remote_write: boolean;
  providers: Record<ProviderId, ProviderReadiness>;
  agent_readiness: Record<AgentSurface, ProviderReadiness>;
}

export interface WritingSession {
  provider: ProviderId;
  native_session_id: string;
  execution_machine: string;
  project_id: string;
  title?: string | null;
  model: string;
  reasoning?: string | null;
  created_at: string;
  last_resumed_at: string;
  introduction_hash_examined: string;
  graph_revision_examined: number;
  research_md_hash_examined: string;
}
