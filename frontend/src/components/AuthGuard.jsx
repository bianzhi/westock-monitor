import { useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { setAuth, getAuth } from '../stores/auth';
import { setAuthToken } from '../api';

export default function AuthGuard({ children }) {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(!!getAuth().user);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isSignUp, setIsSignUp] = useState(false);
  const [sending, setSending] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    if (!supabase || !supabase.auth) {
      setLoading(false);
      setAuthed(true);
      return;
    }
    supabase.auth.getSession().then(({ data }) => {
      const user = data?.session?.user ?? null;
      setAuth(user, data?.session ?? null);
      setAuthToken(data?.session?.access_token || null);
      setAuthed(!!user);
      setLoading(false);
    }).catch(() => setLoading(false));

    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      const user = session?.user ?? null;
      setAuth(user, session);
      setAuthToken(session?.access_token || null);
      setAuthed(!!user);
    });
    return () => data?.subscription?.unsubscribe();
  }, []);

  const handleSubmit = async () => {
    if (!email || !password || !supabase) return;
    setSending(true);
    setMsg('');
    try {
      let error;
      if (isSignUp) {
        ({ error } = await supabase.auth.signUp({ email, password }));
        if (!error) setMsg('注册成功，已自动登录。');
      } else {
        ({ error } = await supabase.auth.signInWithPassword({ email, password }));
      }
      if (error) setMsg(error.message);
    } catch (e) {
      setMsg(e.message || '操作失败');
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
        <h3>{isSignUp ? '注册' : '登录'} Westock Monitor</h3>
        <input
          type="email"
          placeholder="邮箱"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={{
            width: '100%', padding: '8px 12px', fontSize: 14,
            border: '1px solid #d9d9d9', borderRadius: 6, marginBottom: 12,
          }}
        />
        <input
          type="password"
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
          style={{
            width: '100%', padding: '8px 12px', fontSize: 14,
            border: '1px solid #d9d9d9', borderRadius: 6, marginBottom: 12,
          }}
        />
        <button
          onClick={handleSubmit}
          disabled={sending || !email || !password}
          style={{
            width: '100%', padding: '10px 0', fontSize: 15,
            background: sending ? '#ccc' : '#e74c3c', color: '#fff',
            border: 'none', borderRadius: 6, cursor: sending ? 'not-allowed' : 'pointer',
          }}
        >
          {sending ? '处理中...' : (isSignUp ? '注册' : '登录')}
        </button>
        {msg && (
          <p style={{ marginTop: 12, color: msg.includes('失败') || msg.includes('Invalid') ? '#e74c3c' : '#27ae60', fontSize: 13 }}>
            {msg}
          </p>
        )}
        <p style={{ marginTop: 16, fontSize: 13, color: '#666' }}>
          {isSignUp ? '已有账号？' : '没有账号？'}
          <a onClick={() => { setIsSignUp(!isSignUp); setMsg(''); }} style={{ color: '#e74c3c', cursor: 'pointer', marginLeft: 4 }}>
            {isSignUp ? '去登录' : '去注册'}
          </a>
        </p>
      </div>
    );
  }

  return children;
}
