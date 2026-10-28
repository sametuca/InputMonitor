# InputMonitor

Windows tuş kaydı: düşük seviye klavye kancası + isteğe bağlı pano takibi. Tek dosya motor (`input_monitor.py`), üstünde Tkinter kontrol paneli (`input_monitor_ui.py`). Harici bağımlılık yok.

Ad, aracın kullanım amacını yansıtır: kurumsal/kişisel cihazlarda **girdi izleme** — kendi makinenizde, yetkiniz dahilindeki süreçlerin klavye/pano hareketlerini kaydetmek, sonra incelemek veya dışa aktarmak için. Kayıt tamamen yereldir; hiçbir ağ bağlantısı kurulmaz.

## Hızlı başlangıç

```bat
:: paneli aç
pythonw input_monitor_ui.py

:: panel olmadan, arka planda
pythonw input_monitor.py --log C:\logs\keys.txt
```

Panelden yapabildikleriniz:

- **Başlat / Durdur** (veya `F5`) — kancayı açar/kapatır; ayarlar yalnızca durmuşken değiştirilebilir.
- **Canlı kayıt** — dosya 400 ms'de bir okunur, otomatik kaydırma kapatılabilir, görünüm 3000 satırla sınırlıdır (dosya sınırsız büyür).
- **Temizle** (`Ctrl+L`) — kayıt dosyasını boşaltır; kayıt sürerken de çalışır.
- **Gözat / Dışa aktar / Klasörü aç** — dosya yolu seçimi, anlık kopya, Explorer'da açma.
- **Oturum açılışında başlat** — `HKCU\...\Run` kaydını ekler/kaldırır, sonucu geri okuyup doğrular.
- Üst şerit: durum, geçen süre, tuş sayısı, dosya boyutu. Alt şerit: pid, kanca durumu.

Panelin kendi penceresine yazılan tuşlar varsayılan olarak **yoksayılır** ("Kendi penceremdeki tuşları yoksay"), böylece paneli kullanırken kayıt kirlenmez.

## Komut satırı

| Bayrak | Etki |
| --- | --- |
| `--log PATH` | Kayıt dosyası (varsayılan: betiğin yanındaki `input-monitor.log`). |
| `--clipboard` | Pano değişikliklerini de yaz (`[CLIPBOARD] ...`, 4000 karakterde kırpılır). |
| `--no-window` | Düz kayıt: pencere/uygulama başlığı ve boşta ayırıcı yok. |
| `--include-injected` | `SendInput`/`keybd_event` ile üretilen sentetik girdiyi de kaydet. |
| `--exclude-self` | Bu sürecin pencerelerine yazılan tuşları yoksay. |
| `--idle N` | Boşta ayırıcı eşiği, saniye (varsayılan 120). |
| `--duration N` | N saniye sonra çık (test için). |
| `--install` / `--uninstall` | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\<ad>` kaydını yönet. |
| `--name VALUE` | Otomatik başlatma değer adı (varsayılan `InputMonitor`). |
| `--debug` | Kanca geri çağrısındaki hataları stderr'e yaz. |

Eşzamanlılık: komut satırı sürümü `Global\im_{…}`, panel `Global\imui_{…}` mutex'ini tutar. Panel kayıt başlatırken arka plandaki oturumu fark eder ve mükerrer yazmayı önlemek için uyarır; kayıt sırasında `Global\im_{…}` mutex'ini kendisi alır, böylece otomatik başlatma aynı anda ikinci bir kanca açamaz.

## Kayıt biçimi

```
===== session 2026-10-28 23:54:33  DESKTOP-X  pid=26840 clipboard=True
[23:54:34] msedge.exe :: Inbox - Google Chrome
hesap: samet<ENTER>
[23:54:41] [CLIPBOARD] ornek pano icerigi: https://example.com/login?token=abc
deneme@example.com<DEL><BACK><LEFT><F1>
===== session end 2026-10-28 23:54:43 panel: durduruldu
```

Yazdırılabilir karakterler olduğu gibi yazılır. Görünmez tuşlar işaretlenir (`<ENTER>`, `<TAB>`, `<BACK>`, `<DEL>`, `<LEFT>`, `<F5>`, `<NUM+>`, …), `Ctrl`+harf çiftleri `^C` biçiminde. `<ENTER>` yeni satır açar. Ön plan penceresi değiştiğinde ve boşluk sonrasında `[saat] uygulama :: başlık` başlığı düşülür, böylece her satır hedef uygulamaya atfedilebilir.

## Nasıl çalışıyor

- **Kanca** — `SetWindowsHookExW(WH_KEYBOARD_LL, …)` kendi iş parçacığında kurulur ve `GetMessageW` döngüsü o iş parçacığında döner (geri çağrı yalnızca kancayı kuran iş parçacığında tetiklenir). Kapatma, o iş parçacığına `WM_QUIT` gönderip kancayı çözer; `Engine` bu yüzden yeniden başlatılabilir.
- **Çeviri** — ön plandaki iş parçacığının düzeniyle `ToUnicodeEx(vk, scan, keyState, …)`. Tuş durumu `GetAsyncKeyState`/`GetKeyState`'ten (Shift, Ctrl, Alt, CapsLock, NumLock) kurulur; düşük seviye kancada `GetKeyboardState` güvenilir değildir. `ToUnicodeEx` `-1` dönerse ölü tuş saklanır ve bir sonraki tuştan önce tekrar oynatılır, böylece aksan birleşimleri doğru çıkar.
- **Bağlam** — her tuşta `GetForegroundWindow` → `GetWindowTextW` → `QueryFullProcessImageNameW`. Başlık yalnızca pencere gerçekten değişince yazılır.
- **Pano** — `GetClipboardSequenceNumber` değişimini izleyen iş parçacığı, `OpenClipboard` kilitlenmelerine karşı tekrar deneyerek `CF_UNICODETEXT` okur.
- **Sağlamlık** — kanca geri çağrısı tümüyle sarmalanmıştır (içinde kaçan istisna pompayı bozar); her yazımdan sonra `flush`, UTF-8, ekleme kipi.
- **Panel** — tüm Tk çağrıları ana iş parçacığında; dosya 400 ms'de bir artımlı okunur (`Tail`), kesilme ve çok baytlı karakter bölünmesi tolere edilir.

## Notlar

- Kanca masaüstü/oturum başına çalışır. Gözlemlemek istediğiniz etkileşimli oturumda normal kullanıcı süreci olarak başlatın; hizmet (session 0) olarak çalıştırıldığında hiçbir tuş görmez.
- UIPI, orta bütünlükteki bir sürecin yükseltilmiş pencerelere girdi göndermesini engeller; kanca tuşları yine de görür.
- Kayıt düz metindir ve sınırsız büyür. `--duration` + zamanlanmış görev ya da panelden **Temizle** ile döndürün.
- `--install` yalnızca `Run` değeri yazar; başka hiçbir şeyi değiştirmez. Doğrulama: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v InputMonitor`.
