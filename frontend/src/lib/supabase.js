import { createClient } from '@supabase/supabase-js';

let _supabase = null;

function getSupabase() {
  if (_supabase) return _supabase;

  const url = import.meta.env.VITE_SUPABASE_URL;
  const key = import.meta.env.VITE_SUPABASE_ANON_KEY;

  if (!url || !key) {
    console.warn('[westock] Supabase 环境变量缺失，千人千面功能不可用。请在 .env.production 中设置 VITE_SUPABASE_URL 和 VITE_SUPABASE_ANON_KEY。');
    return null;
  }

  _supabase = createClient(url, key, {
    auth: {
      autoRefreshToken: true,
      persistSession: true,
      storageKey: 'westock-web-auth',
    },
  });
  return _supabase;
}

export const supabase = new Proxy({}, {
  get(_target, prop) {
    const client = getSupabase();
    if (!client) return undefined;
    return client[prop];
  },
});
