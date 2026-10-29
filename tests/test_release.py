import asyncio,json,threading,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
import capture_app as app
from official_mark import RegistrationSession,classified_labels

class ReleaseTests(unittest.TestCase):
    def test_capture_delay(self):
        self.assertEqual(app.capture_delay({}),.3)
        with self.assertRaises(ValueError):app.capture_delay({'capture_delay_ms':-1})
    def test_sound_assets(self):
        bank=json.loads((app.SOUNDS/'manifest.json').read_text(encoding='utf8'))
        self.assertEqual(len(bank['success']),17)
        clips=[c for v in bank['success'].values() for c in v['clips']]+bank['failure']+bank['capture']
        self.assertEqual(len(clips),66)
        for clip in clips:app.sound_path(clip)
    def test_separate_volume(self):
        values={'voice_volume':25,'capture_volume':75}
        self.assertEqual(app.clip_volume(values,{'file':'success/test.wav'}),25)
        self.assertEqual(app.clip_volume(values,{'file':'capture/test.wav'}),75)
    def test_puzzle_and_mora_categories(self):
        labels,_=classified_labels([{'id':186,'children':[{'id':713,'name':'Assembly Modules'}]},
                                   {'id':51,'children':[{'id':723,'name':'Regional container'}]}])
        self.assertEqual(labels,{713:'chest',723:'mora'})

class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_authenticated_read(self):
        session=RegistrationSession();session.open=AsyncMock();session.page=Mock();session.page.is_closed.return_value=False
        with patch('official_mark.OfficialMap') as client,patch('official_mark.asyncio.sleep',new_callable=AsyncMock):
            client.return_value.read=AsyncMock(side_effect=[RuntimeError('Please login'),set()])
            await session.ensure_login(threading.Event())
            self.assertEqual(client.return_value.read.await_count,2)
    async def test_closed_map_is_not_logged_in(self):
        session=RegistrationSession();session.open=AsyncMock();session.page=Mock();session.page.is_closed.return_value=True
        with self.assertRaises(RuntimeError):await session.ensure_login(threading.Event())

if __name__=='__main__':unittest.main()
