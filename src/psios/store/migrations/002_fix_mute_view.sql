-- 002: mute 过滤改用 rowid(插入序)而非秒级时间戳比较。
-- 原视图在 mute 与 unmute 发生于同一秒时无法恢复可见性。
-- 语义: unmute 记录插入序晚于 mute 即解除屏蔽(human_action 为 append-only)。

DROP VIEW IF EXISTS v_interest_current;

CREATE VIEW v_interest_current AS    -- 过滤 merged/retired topic 与 muted 后的当前兴趣
  SELECT i.*, t.label, t.status AS topic_status
  FROM interest i JOIN topic t ON t.id = i.topic_id
  WHERE t.status = 'active'
    AND NOT EXISTS (
      SELECT 1 FROM human_action h
      WHERE h.action_type = 'mute_interest'
        AND h.target_id = i.topic_id
        AND NOT EXISTS (
          SELECT 1 FROM human_action u
          WHERE u.action_type = 'unmute_interest'
            AND u.target_id = i.topic_id
            AND u.rowid > h.rowid));
