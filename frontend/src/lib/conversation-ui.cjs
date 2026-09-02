'use strict';

const MAX_VARIABLES = 50;
const MAX_VARIABLE_KEY_LENGTH = 100;
const MAX_VARIABLE_STRING_LENGTH = 1_000;
const MAX_VARIABLE_BYTES = 16_384;

function parseTestVariables(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('Pre-call variables must be valid JSON. Check commas, quotes, and brackets.');
  }

  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new Error('Pre-call variables must be a JSON object, for example {"customer_name":"Vimal"}.');
  }

  const entries = Object.entries(value);
  if (entries.length > MAX_VARIABLES) {
    throw new Error(`Pre-call variables can contain at most ${MAX_VARIABLES} values.`);
  }

  for (const [key, item] of entries) {
    if (!key || key.length > MAX_VARIABLE_KEY_LENGTH) {
      throw new Error(`Variable keys must contain 1–${MAX_VARIABLE_KEY_LENGTH} characters.`);
    }
    if (key === '_vav_call_id' || key.startsWith('_voice_ai_')) {
      throw new Error(`“${key}” is reserved by the platform.`);
    }
    if (!['string', 'number', 'boolean'].includes(typeof item)) {
      throw new Error(`“${key}” must be a string, number, or boolean.`);
    }
    if (typeof item === 'number' && !Number.isFinite(item)) {
      throw new Error(`“${key}” must be a finite number.`);
    }
    if (typeof item === 'string' && item.length > MAX_VARIABLE_STRING_LENGTH) {
      throw new Error(`“${key}” can contain at most ${MAX_VARIABLE_STRING_LENGTH} characters.`);
    }
  }

  if (new TextEncoder().encode(JSON.stringify(value)).length > MAX_VARIABLE_BYTES) {
    throw new Error('Pre-call variables can contain at most 16 KB of data.');
  }

  return value;
}

function sessionErrorGuidance(error) {
  const name = typeof error === 'object' && error && typeof error.name === 'string'
    ? error.name
    : '';
  const rawMessage = error instanceof Error
    ? error.message
    : typeof error === 'object' && error && typeof error.message === 'string'
      ? error.message
      : '';
  const message = rawMessage.toLowerCase();

  if (name === 'NotAllowedError' || message.includes('permission') || message.includes('notallowed')) {
    return {
      title: 'Microphone permission is blocked',
      message: 'Allow microphone access for this site in your browser, then retry with a fresh secure token.',
    };
  }
  if (name === 'NotFoundError' || message.includes('requested device not found')) {
    return {
      title: 'No microphone was found',
      message: 'Connect or enable a microphone, confirm it is available to the browser, then retry.',
    };
  }
  if (name === 'NotReadableError' || message.includes('could not start audio source')) {
    return {
      title: 'Microphone is unavailable',
      message: 'Close other apps using the microphone, check the selected input device, then retry.',
    };
  }
  if (name === 'SecurityError' || message.includes('secure context')) {
    return {
      title: 'Secure browser access is required',
      message: 'Open this workspace over HTTPS and allow browser media access before retrying.',
    };
  }
  if (
    message.includes('token')
    || message.includes('expired')
    || message.includes('unauthorized')
    || message.includes('401')
  ) {
    return {
      title: 'Secure session token was not accepted',
      message: 'Retry to request a new single-use token. Tokens are obtained only after microphone permission is ready.',
    };
  }
  if (
    message.includes('websocket')
    || message.includes('network')
    || message.includes('failed to fetch')
    || message.includes('load failed')
  ) {
    return {
      title: 'Could not reach the voice service',
      message: 'Check your connection and firewall access, then retry. A fresh single-use token will be requested.',
    };
  }

  return {
    title: 'Voice session could not start',
    message: rawMessage || 'Confirm the agent is published and retry. If it continues, check provider status.',
  };
}

function valueFromRecord(record, keys) {
  if (!record || typeof record !== 'object') return '';
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function transcriptLanguage(turn) {
  const direct = valueFromRecord(turn, ['language', 'language_code', 'locale', 'detected_language']);
  if (direct) return direct;
  const metadata = turn && typeof turn === 'object' && turn.metadata;
  return valueFromRecord(metadata, ['language', 'language_code', 'locale', 'detected_language']);
}

function reduceTranscriptState(state, action) {
  if (action.type === 'clear') return { turns: [], live: null };
  if (action.type === 'clear_live') return { ...state, live: null };
  if (action.type === 'delta') {
    const text = typeof action.text === 'string' ? action.text.trim() : '';
    if (!text) return state;
    return {
      ...state,
      live: {
        role: action.role,
        text,
        ...(typeof action.language === 'string' && action.language.trim()
          ? { language: action.language.trim() }
          : {}),
      },
    };
  }
  if (action.type === 'settled') {
    const text = typeof action.text === 'string' ? action.text.trim() : '';
    if (!text) return state;
    return {
      turns: [
        ...state.turns,
        {
          id: action.id,
          role: action.role,
          text,
          ...(typeof action.language === 'string' && action.language.trim()
            ? { language: action.language.trim() }
            : {}),
        },
      ],
      live: state.live?.role === action.role ? null : state.live,
    };
  }
  return state;
}

module.exports = {
  parseTestVariables,
  reduceTranscriptState,
  sessionErrorGuidance,
  transcriptLanguage,
  valueFromRecord,
};
