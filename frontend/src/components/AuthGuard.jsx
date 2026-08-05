import { useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { setAuth, getAuth } from '../stores/auth';
import { setAuthToken } from '../api';

/**
 * 登录守卫：检查 Supabase 会话状态，未登录显示登录框，已登录透传 children。
 * Supabase 未配置时跳过认证（兼容无 Supabase 的部署）。
 */
export default function AuthGuard({ children }) {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(!!getAuth().user);
  const [email, setEmail] = useState('');
  const [sending, setSending] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    if (!supabase || !supabase.auth) {
      setLoading(false);
      setAuthed(true); // 无 Supabase 时不拦截
      return;
    }

    supabase.auth.getSession().then(({ data }) => {
      const user = data?.session?.user ?? null;
      setAuth(user, data?.session ?? null);
      setAuthed(!!user);
      setLoading(false);
    }).catch(() => {
      setLoading(false);
    });

    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      const user = session?.user ?? null;
      setAuth(user, session);
      setAuthed(!!user);
      setAuthToken(session?.access_token || null);
    });

    return () => data?.subscription?.unsubscribe();
  }, []);

  const handleLogin = async () => {
    if (!email || !supabase) return;
    setSending(true);
    setMsg('');
    try {
      const { error } = await supabase.auth.signInWithOtp({
        email,
        options: { shouldCreateUser: true },
      });
      if (error) setMsg(error.message);
      else setMsg('已发送登录链接到邮箱，请查收并点击链接登录。');
    } catch (e) {
      setMsg(e.message || '登录失败');
    } finally {
      setSending(false);
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <span style={{ color: '#999' }}>加载中...</span>
      </div>
    );
  }

  if (!authed) {
    return (
      <div style={{ maxWidth: 400, margin: '80px auto', padding: 24, textAlign: 'center' }}>
        <h3>登录 Westock Monitor</h3>
        <p style={{ color: '#666', fontSize: 14, marginBottom: 16 }}>
          输入邮箱获取免密登录链接
        </p>
        <input
          type="email"
          placeholder="your@email.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
          style={{
            width: '100%', padding: '8px 12px', fontSize: 14,
            border: '1px solid #d9d9d9', borderRadius: 6, marginBottom: 12,
          }}
        />
        <button
          onClick={handleLogin}
          disabled={sending || !email}
          style={{
            width: '100%', padding: '10px 0', fontSize: 15,
            background: sending ? '#ccc' : '#e74c3c', color: '#fff',
            border: 'none', borderRadius: 6, cursor: sending ? 'not-allowed' : 'pointer',
          }}
        >
          {sending ? '发送中...' : '发送登录链接'}
        </button>
        {msg && (
          <p style={{ marginTop: 12, color: msg.includes('失败') ? '#e74c3c' : '#27ae60', fontSize: 13 }}>
            {msg}
          </p>
        )}
      </div>
    );
  }

  return children;
}
