package auth

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginapi"
)

func TestSchedulerCandidatesExposeIndependentQuotaSnapshot(t *testing.T) {
	observedAt := time.Unix(1_800_000_000, 0)
	auth := &Auth{
		ID:       "account",
		Provider: "codex",
		Attributes: map[string]string{
			"label":        "account",
			"access_token": "attribute-secret",
		},
		Metadata: map[string]any{"access_token": "metadata-secret"},
		Quota: QuotaState{
			Exceeded:      true,
			NextRecoverAt: observedAt.Add(time.Hour),
			ObservedAt:    observedAt,
			Signals:       map[string]string{"X-Codex-Primary-Used-Percent": "90"},
		},
	}
	candidates := schedulerAuthCandidates([]*Auth{nil, auth})
	if len(candidates) != 1 {
		t.Fatalf("got %d candidates, want 1", len(candidates))
	}
	raw, errMarshal := json.Marshal(candidates)
	if errMarshal != nil {
		t.Fatal(errMarshal)
	}
	for _, secret := range []string{"attribute-secret", "metadata-secret", "next_recover_at", "exceeded"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("scheduler snapshot leaked %q", secret)
		}
	}
	var decoded []pluginapi.SchedulerAuthCandidate
	if errUnmarshal := json.Unmarshal(raw, &decoded); errUnmarshal != nil {
		t.Fatal(errUnmarshal)
	}
	if !decoded[0].Quota.ObservedAt.Equal(observedAt) || decoded[0].Quota.Signals["X-Codex-Primary-Used-Percent"] != "90" {
		t.Fatalf("quota did not survive JSON transport: %#v", decoded[0].Quota)
	}
	candidates[0].Quota.Signals["X-Codex-Primary-Used-Percent"] = "100"
	if auth.Quota.Signals["X-Codex-Primary-Used-Percent"] != "90" {
		t.Fatal("scheduler mutated the host quota snapshot")
	}
}
