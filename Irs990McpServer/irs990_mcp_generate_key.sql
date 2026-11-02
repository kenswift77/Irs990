-- ============================================================================
-- Generate a new MCP client API key
--
-- Usage:
--   1. Run this script — it prints the plain-text key ONCE.
--   2. Copy the key and give it to the client administrator.
--   3. The plain-text key is never stored; only the SHA-256 hash is kept.
--
-- To revoke access: UPDATE dbo.mcp_clients SET is_active = 0 WHERE id = <id>
-- To rotate key:    Run this script again for the same client_name, then
--                   DELETE the old row and have the client update their config.
-- ============================================================================

DECLARE @client_name NVARCHAR(200) = N'Net Outlook LLC';  -- <-- change this
DECLARE @notes       NVARCHAR(500) = NULL;                      -- optional

-- Generate a cryptographically random 32-byte key, base64-encoded (~44 chars)
DECLARE @raw_key    VARCHAR(64)  = CONVERT(VARCHAR(64), CRYPT_GEN_RANDOM(32), 2); -- hex string
DECLARE @prefix     VARCHAR(12)  = LEFT(@raw_key, 8);
DECLARE @key_hash   CHAR(64)     = CONVERT(CHAR(64), HASHBYTES('SHA2_256', @raw_key), 2);

INSERT INTO dbo.mcp_clients (client_name, api_key_hash, api_key_prefix, notes)
VALUES (@client_name, @key_hash, @prefix, @notes);

DECLARE @new_id INT = SCOPE_IDENTITY();

-- Print the plain-text key — this is the ONLY time you will ever see it.
PRINT '========================================================';
PRINT 'NEW MCP CLIENT CREATED';
PRINT '========================================================';
PRINT 'Client ID   : ' + CAST(@new_id AS VARCHAR);
PRINT 'Client Name : ' + @client_name;
PRINT 'Key Prefix  : ' + @prefix + '...';
PRINT '';
PRINT 'PLAIN-TEXT API KEY (copy this now — it will not be shown again):';
PRINT @raw_key;
PRINT '========================================================';

SELECT @new_id AS client_id, @client_name AS client_name,
       @prefix AS api_key_prefix, @raw_key AS plain_text_key_SAVE_THIS;
