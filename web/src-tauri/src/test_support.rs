//! Shared waits for async tests: one bounded poll instead of a loop per test,
//! so timing lives in one place and a timeout names what it waited for.

use std::time::{Duration, Instant};

pub(crate) const TEST_WAIT_TIMEOUT: Duration = Duration::from_secs(2);
pub(crate) const TEST_WAIT_INTERVAL: Duration = Duration::from_millis(20);

/// Poll `condition` until it returns `Ok(())`. After `TEST_WAIT_TIMEOUT` the
/// test fails with `description` and the condition's last `Err` detail.
pub(crate) async fn wait_until<F>(description: &str, mut condition: F)
where
    F: FnMut() -> Result<(), String>,
{
    let deadline = Instant::now() + TEST_WAIT_TIMEOUT;
    loop {
        match condition() {
            Ok(()) => return,
            Err(detail) => {
                assert!(Instant::now() < deadline, "{description}: {detail}");
                tokio::time::sleep(TEST_WAIT_INTERVAL).await;
            }
        }
    }
}
