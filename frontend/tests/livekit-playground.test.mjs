import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const playgroundSource = readFileSync(new URL('../src/pages/playground.tsx', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const packageJson = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

test('LiveKit browser SDK is pinned and loaded only after a user starts a session', () => {
  assert.equal(packageJson.dependencies['livekit-client'], '2.22.0');
  assert.match(playgroundSource, /await import\('livekit-client'\)/);
  assert.doesNotMatch(playgroundSource, /^import (?!type\b).*from 'livekit-client';/m);

  const microphoneIndex = playgroundSource.indexOf('await requestMicrophoneReadiness()');
  const tokenIndex = playgroundSource.indexOf('api.createLiveKitBrowserSession(selected.id, variables)');
  assert.ok(microphoneIndex > -1 && tokenIndex > microphoneIndex, 'token must be requested after microphone permission');
});

test('microphone permission is bounded and a late stream is always released', () => {
  const readinessStart = playgroundSource.indexOf('async function requestMicrophoneReadiness()');
  const readinessEnd = playgroundSource.indexOf('function isLiveKitBrowserLlmProvider', readinessStart);
  const readinessSource = playgroundSource.slice(readinessStart, readinessEnd);

  assert.match(playgroundSource, /const MICROPHONE_PERMISSION_TIMEOUT_MS = 25_000/);
  assert.match(readinessSource, /Promise\.race\(\[permissionRequest, permissionTimeout\]\)/);
  assert.match(readinessSource, /permissionTimedOut = true/);
  assert.match(readinessSource, /Click Allow in the browser prompt or site settings/);
  assert.match(readinessSource, /permissionRequest\.then\(\(lateStream\) => \{/);
  assert.match(readinessSource, /lateStream\.getTracks\(\)\.forEach\(\(track\) => track\.stop\(\)\)/);
  assert.match(playgroundSource, /Waiting for microphone permission — click Allow in the browser prompt/);
});

test('LiveKit browser candidates require one of the supported LLM providers', () => {
  assert.match(
    playgroundSource,
    /return provider === 'openai' \|\| provider === 'inworld'/,
  );
  assert.equal(
    playgroundSource.match(/isLiveKitBrowserLlmProvider\([^)]*\.llm_provider\)/g)?.length,
    2,
  );
});

test('LiveKit browser token uses the authenticated agent endpoint and stays memory-only', () => {
  assert.match(apiSource, /`\/api\/v1\/agents\/\$\{agentId\}\/livekit\/session`/);
  assert.match(apiSource, /body: JSON\.stringify\(\{ variables \}\)/);
  assert.match(apiSource, /headers: \{ 'Idempotency-Key': idempotencyKey \}/);
  assert.match(apiSource, /globalThis\.crypto\?\.randomUUID/);
  assert.doesNotMatch(playgroundSource, /localStorage|sessionStorage|document\.cookie/);
  assert.doesNotMatch(playgroundSource, /access_token\}\)|access_token\}</);
  assert.match(playgroundSource, /'room-scoped join token'/);
});

test('active Inworld and LiveKit agents expose independent browser and phone tests', () => {
  assert.match(playgroundSource, /selected\?\.voice_provider === 'inworld'/);
  assert.match(playgroundSource, /selectedRuntimeProfile\.telephony_provider === 'livekit_sip'/);
  assert.match(playgroundSource, /selectedRuntimeProfile\.primary_speech_provider === 'inworld'/);
  assert.match(playgroundSource, /selectedRuntimeProfile\.status !== 'inactive'/);
  assert.match(playgroundSource, /const browserTestAvailable = browserTransport === 'livekit'\s*\? selectedUsesLiveKitBrowser/);
  assert.match(playgroundSource, /const phoneTestReady = selectedReady/);
  assert.match(playgroundSource, /'Test in browser'/);
  assert.match(playgroundSource, /Call assigned number/);
  assert.match(playgroundSource, /Browser test does not use the e&amp; carrier line/);
});

test('LiveKit session reports audio, agent state, transcript, lifecycle, and identifiers', () => {
  assert.match(playgroundSource, /RoomEvent\.TrackSubscribed/);
  assert.match(playgroundSource, /registerTextStreamHandler\('lk\.transcription'/);
  assert.match(playgroundSource, /\['lk\.segment_id'\]/);
  assert.match(playgroundSource, /\['lk\.transcription_final'\]/);
  assert.match(playgroundSource, /RoomEvent\.ParticipantAttributesChanged/);
  assert.match(playgroundSource, /\['lk\.agent\.state'\]/);
  assert.match(playgroundSource, /RoomEvent\.Reconnecting/);
  assert.match(playgroundSource, /RoomEvent\.Disconnected/);
  assert.match(playgroundSource, /roomName: session\.room_name/);
  assert.match(playgroundSource, /participantIdentity: session\.participant_identity/);
  assert.match(playgroundSource, /callId: session\.call_id/);
  assert.match(playgroundSource, /setMicrophoneEnabled\(!targetMuted\)/);
  assert.match(playgroundSource, /End browser test/);
});

test('LiveKit worker absence and unexpected agent departure fail visibly and close the room', () => {
  assert.match(playgroundSource, /const AGENT_JOIN_TIMEOUT_MS = 20_000/);
  assert.match(playgroundSource, /RoomEvent\.ParticipantDisconnected/);
  assert.match(playgroundSource, /LiveKit agent did not join/);
  assert.match(playgroundSource, /LiveKit agent disconnected/);
  assert.match(playgroundSource, /armAgentJoinTimeout\(\)/);
});

test('all LiveKit transitional states keep playground controls locked', () => {
  assert.match(
    playgroundSource,
    /\['connecting', 'initializing', 'listening', 'thinking', 'speaking', 'reconnecting'\]\.includes\(state\)/,
  );
});

test('terminal LiveKit failures tear down the current room and microphone before retry', () => {
  const teardownStart = playgroundSource.indexOf('const teardownLiveKitRoom = () =>');
  const teardownEnd = playgroundSource.indexOf('const applyAgentState', teardownStart);
  const teardownSource = playgroundSource.slice(teardownStart, teardownEnd);
  assert.match(teardownSource, /liveKitRoomRef\.current = null/);
  assert.match(teardownSource, /track\) => track\.detach\(\)/);
  assert.match(teardownSource, /setMicrophoneEnabled\(false\)/);
  assert.match(teardownSource, /room\.disconnect\(true\)/);

  const failedStateStart = playgroundSource.indexOf("if (mappedState === 'error')");
  const failedStateEnd = playgroundSource.indexOf("} else if (mappedState === 'ended')", failedStateStart);
  assert.match(playgroundSource.slice(failedStateStart, failedStateEnd), /teardownLiveKitRoom\(\)/);

  const mediaErrorStart = playgroundSource.indexOf('room.on(RoomEvent.MediaDevicesError');
  const mediaErrorEnd = playgroundSource.indexOf('room.on(RoomEvent.Disconnected', mediaErrorStart);
  assert.match(playgroundSource.slice(mediaErrorStart, mediaErrorEnd), /teardownLiveKitRoom\(\)/);

  const outerCatchStart = playgroundSource.indexOf('} catch (sessionError) {');
  const outerCatchEnd = playgroundSource.indexOf('} finally {', outerCatchStart);
  const outerCatchSource = playgroundSource.slice(outerCatchStart, outerCatchEnd);
  assert.match(outerCatchSource, /terminalStateRef\.current === 'ended'/);
  assert.match(outerCatchSource, /setMicrophoneEnabled\(false\)/);
  assert.match(outerCatchSource, /room\.disconnect\(true\)/);
});

test('ending while browser setup is pending prevents later token or room activation', () => {
  assert.match(playgroundSource, /await requestMicrophoneReadiness\(\);\s*if \(terminalStateRef\.current === 'ended' \|\| terminalStateRef\.current === 'error'\) return;/);
  assert.match(playgroundSource, /await import\('livekit-client'\);\s*if \(terminalStateRef\.current === 'ended'\) return;/);
  assert.match(playgroundSource, /await api\.createLiveKitBrowserSession\(selected\.id, variables\);\s*if \(terminalStateRef\.current === 'ended'\) return;/);
});

test('navigation, rapid mute, reconnect, and unsolicited disconnect stay fail closed', () => {
  assert.match(playgroundSource, /useEffect\(\(\) => \(\) => \{\s*terminalStateRef\.current = 'ended'/);
  assert.match(playgroundSource, /if \(muteOperationRef\.current\) return/);
  assert.match(playgroundSource, /if \(liveKitRoomRef\.current !== room\)/);
  assert.match(playgroundSource, /setMicrophoneEnabled\(false\)\.catch/);

  const reconnectedStart = playgroundSource.indexOf('room.on(RoomEvent.Reconnected');
  const reconnectedEnd = playgroundSource.indexOf('room.on(RoomEvent.AudioPlaybackStatusChanged', reconnectedStart);
  assert.match(playgroundSource.slice(reconnectedStart, reconnectedEnd), /armAgentJoinTimeout\(\)/);

  const disconnectedStart = playgroundSource.indexOf('room.on(RoomEvent.Disconnected');
  const disconnectedEnd = playgroundSource.indexOf('const timeout = timeoutAfter', disconnectedStart);
  const disconnectedSource = playgroundSource.slice(disconnectedStart, disconnectedEnd);
  assert.match(disconnectedSource, /LiveKit session disconnected/);
  assert.match(disconnectedSource, /reachedMaximumDuration/);
  assert.match(disconnectedSource, /setState\(terminalStateRef\.current === 'error' \? 'error' : 'ended'\)/);
});

test('agent switches and stale provider or audio callbacks cannot control a newer session', () => {
  const selectionResetStart = playgroundSource.indexOf('// Router-driven agent changes');
  const selectionResetEnd = playgroundSource.indexOf('const primary = selected?.language', selectionResetStart);
  const selectionResetSource = playgroundSource.slice(selectionResetStart, selectionResetEnd);
  assert.match(selectionResetSource, /terminalStateRef\.current = 'ended'/);
  assert.match(selectionResetSource, /setMicrophoneEnabled\(false\)/);
  assert.match(selectionResetSource, /previousRoom\.disconnect\(true\)/);

  assert.match(playgroundSource, /const isCurrentSmallestAgent = \(\) => smallestAgentRef\.current === voiceAgent/);
  assert.match(playgroundSource, /voiceAgent\.on\('session_started',[\s\S]*?if \(!isCurrentSmallestAgent\(\) \|\| terminalStateRef\.current !== null\)/);
  assert.match(playgroundSource, /voiceAgent\.on\('session_ended',[\s\S]*?if \(!isCurrentSmallestAgent\(\)\) return/);

  const outerCatchStart = playgroundSource.indexOf('} catch (sessionError) {');
  const outerCatchEnd = playgroundSource.indexOf('} finally {', outerCatchStart);
  assert.match(
    playgroundSource.slice(outerCatchStart, outerCatchEnd),
    /terminalStateRef\.current === 'ended' \|\| terminalStateRef\.current === 'error'/,
  );

  const trackStart = playgroundSource.indexOf('room.on(RoomEvent.TrackSubscribed');
  const trackEnd = playgroundSource.indexOf('room.on(RoomEvent.TrackUnsubscribed', trackStart);
  assert.match(playgroundSource.slice(trackStart, trackEnd), /remoteAudioRef\.current !== audioElement/);
  const enableAudioStart = playgroundSource.indexOf('const enableLiveKitAudio = async');
  const enableAudioEnd = playgroundSource.indexOf('const switchTargetOptions', enableAudioStart);
  assert.match(playgroundSource.slice(enableAudioStart, enableAudioEnd), /liveKitRoomRef\.current !== room/);
});

test('every LiveKit room event is fenced from a superseded retry', () => {
  const guardedEvents = [
    'Connected',
    'ParticipantConnected',
    'ParticipantDisconnected',
    'ParticipantAttributesChanged',
    'Reconnecting',
    'Reconnected',
    'AudioPlaybackStatusChanged',
    'TrackSubscribed',
    'TrackUnsubscribed',
    'DataReceived',
    'LocalAudioSilenceDetected',
    'MediaDevicesError',
    'Disconnected',
  ];
  for (const [index, eventName] of guardedEvents.entries()) {
    const handlerStart = playgroundSource.indexOf(`room.on(RoomEvent.${eventName}`);
    const nextEventName = guardedEvents[index + 1];
    const handlerEnd = nextEventName
      ? playgroundSource.indexOf(`room.on(RoomEvent.${nextEventName}`, handlerStart)
      : playgroundSource.indexOf('const timeout = timeoutAfter', handlerStart);
    assert.ok(handlerStart > -1 && handlerEnd > handlerStart, `${eventName} handler should exist`);
    assert.match(
      playgroundSource.slice(handlerStart, handlerEnd),
      /isCurrentRoom\(\)|liveKitRoomRef\.current !== room/,
      `${eventName} must ignore superseded room events`,
    );
  }
  const transcriptionHandlerStart = playgroundSource.indexOf("room.registerTextStreamHandler('lk.transcription'");
  const transcriptionHandlerEnd = playgroundSource.indexOf('room.on(RoomEvent.DataReceived', transcriptionHandlerStart);
  assert.ok(transcriptionHandlerStart > -1 && transcriptionHandlerEnd > transcriptionHandlerStart);
  assert.match(
    playgroundSource.slice(transcriptionHandlerStart, transcriptionHandlerEnd),
    /isCurrentRoom\(\)/,
    'transcription stream must ignore superseded room data',
  );
});

test('candidate copy does not claim live readiness before the token endpoint succeeds', () => {
  assert.match(playgroundSource, /browser test candidate/);
  assert.match(playgroundSource, /live checks run when you start/);
  assert.match(playgroundSource, /'checks pending'/);
  assert.match(playgroundSource, /Use the conversation to exercise LiveKit, Inworld, VAV knowledge, tools/);
});
