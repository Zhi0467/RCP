import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { loadSpaceMachines } from "../../src/core/api";
import type { SpaceMachine } from "../../src/core/types";
import { MachineCard } from "../../src/projects/MachineCard";
import { SignInsFold, useProviderSignIns } from "../../src/projects/ProviderLogins";
import "../../src/styles.css";

/** One space machine card with its Sign-ins fold; the test routes every API it reads. */
function Harness() {
  const [record, setRecord] = useState<SpaceMachine | null>(null);
  const data = useProviderSignIns("u-me");
  useEffect(() => {
    void loadSpaceMachines().then((machines) => setRecord(machines[0]));
  }, []);
  return (
    <section className="settings-page" data-settings-level="space">
      <section className="settings-section provider-path-settings space-machine-settings">
        <div className="provider-machine-list">
          {record && (
            <MachineCard
              title={record.name}
              hostLabel={record.host}
              osAccount={record.os_account}
              record={record}
              onRecordChange={setRecord}
              signIns={
                <SignInsFold
                  machineName={record.name}
                  machineId={record.machine_id}
                  data={data}
                  viewerUserId="u-me"
                  writesDisabled={false}
                  focusProvider={null}
                  onLoginChanged={() => undefined}
                />
              }
            />
          )}
        </div>
      </section>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
