export interface RedisConnectionOptions {
  host: string;
  port: number;
  password?: string;
  username?: string;
}

export function parseRedisUrl(
  redisUrl?: string,
): RedisConnectionOptions | null {
  if (!redisUrl) return null;
  try {
    const url = new URL(redisUrl);
    return {
      host: url.hostname,
      port: parseInt(url.port || '6379', 10),
      password: url.password || undefined,
      username: url.username !== 'default' ? url.username : undefined,
    };
  } catch {
    return null;
  }
}

/**
 * Get Redis connection options from REDIS_URL or individual env vars.
 * Use with ConfigService: getRedisConnection(configService)
 * Use without ConfigService: getRedisConnection()
 */
export function getRedisConnection(configService?: {
  get: <T>(key: string, defaultValue?: T) => T;
}): RedisConnectionOptions {
  const redisUrl = configService
    ? configService.get<string>('REDIS_URL', '')
    : process.env.REDIS_URL;

  const parsed = parseRedisUrl(redisUrl);
  if (parsed) return parsed;

  if (configService) {
    return {
      host: configService.get<string>('REDIS_HOST', 'localhost'),
      port: configService.get<number>('REDIS_PORT', 6379),
      password: configService.get<string>('REDIS_PASSWORD', '') || undefined,
    };
  }

  return {
    host: process.env.REDIS_HOST || 'localhost',
    port: parseInt(process.env.REDIS_PORT || '6379', 10),
    password: process.env.REDIS_PASSWORD || undefined,
  };
}
